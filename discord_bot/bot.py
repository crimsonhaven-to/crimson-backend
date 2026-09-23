"""Discord gateway bot that lets one operator mint single-use signup invites.

Only the user whose id is DISCORD_OWNER_ID may drive it; everyone else, and every
bot, is ignored. Commands are chat messages with DISCORD_COMMAND_PREFIX
(default ``!``); DM the bot for privacy.

    !invite [n]      mint n one-time invites (1 to 20, default 1)
    !invites         list unused invites
    !revoke <code>   delete an unused invite
    !help, !ping

Hand-rolled over ``websockets`` and ``httpx`` rather than discord.py, to keep
the python:3.14-slim image free of uncertain native dependencies. Run exactly
one instance (``python -m discord_bot``): a second gateway connection with the
same token makes Discord disconnect both repeatedly, and would double-mint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import signal
from typing import List, Optional

import httpx
from websockets.asyncio.client import connect

from account_engine.db import store
from core.config import get_settings

logger = logging.getLogger("discord_bot")

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
REST_BASE = "https://discord.com/api/v10"

# MESSAGE_CONTENT is privileged (Developer Portal, Bot, Privileged Gateway
# Intents) and only needed to read messages in servers: DM content arrives
# regardless, so DMing the bot works with it off.
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_DIRECT_MESSAGES = 1 << 12
INTENT_MESSAGE_CONTENT = 1 << 15
INTENTS = INTENT_GUILDS | INTENT_GUILD_MESSAGES | INTENT_DIRECT_MESSAGES | INTENT_MESSAGE_CONTENT

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

MAX_INVITES_PER_COMMAND = 20
DISCORD_MSG_LIMIT = 2000


class InviteBot:
    def __init__(self, token: str, owner_id: str, prefix: str = "!"):
        self.token = token
        self.owner_id = str(owner_id)
        self.prefix = prefix or "!"
        self.frontend_url = get_settings().frontend_base_url
        self._seq: Optional[int] = None  # last dispatch sequence, echoed in heartbeats
        self._closing = asyncio.Event()
        self._http: Optional[httpx.AsyncClient] = None

    async def run(self) -> None:
        # The bot may start before the API has ever created the schema.
        await asyncio.to_thread(store.init_db)

        async with httpx.AsyncClient(timeout=15.0) as http:
            self._http = http
            backoff = 1
            while not self._closing.is_set():
                try:
                    await self._run_once()
                    backoff = 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - keep the bot alive on any error
                    logger.warning("Gateway connection dropped (%s); reconnecting in %ss", e, backoff)
                if self._closing.is_set():
                    break
                # Sleeps the backoff but wakes immediately on shutdown.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._closing.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60)

        logger.info("Discord bot stopped.")

    def stop(self) -> None:
        self._closing.set()

    def _identify(self) -> str:
        return json.dumps({
            "op": 2,
            "d": {
                "token": self.token,
                "intents": INTENTS,
                "properties": {"os": "linux", "browser": "crimson-invite-bot", "device": "crimson-invite-bot"},
            },
        })

    async def _run_once(self) -> None:
        async with connect(GATEWAY_URL, max_size=2 ** 20) as ws:
            hello = json.loads(await ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000.0
            hb_task = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                await ws.send(self._identify())
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("s") is not None:
                        self._seq = msg["s"]
                    op = msg.get("op")
                    if op == OP_DISPATCH:
                        await self._dispatch(msg)
                    elif op == OP_HEARTBEAT:
                        await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))
                    elif op == OP_RECONNECT:
                        logger.info("Gateway asked us to reconnect.")
                        return
                    elif op == OP_INVALID_SESSION:
                        logger.info("Gateway invalidated the session; re-identifying.")
                        await asyncio.sleep(2)
                        return
            finally:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task

    async def _heartbeat(self, ws, interval: float) -> None:
        # Discord asks for a jittered first beat to avoid thundering herds.
        await asyncio.sleep(interval * random.random())
        while True:
            await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))
            await asyncio.sleep(interval)

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("t")
        d = msg.get("d") or {}
        if t == "READY":
            user = d.get("user") or {}
            logger.info("Connected to Discord as %s#%s (owner=%s)",
                        user.get("username"), user.get("discriminator"), self.owner_id)
        elif t == "MESSAGE_CREATE":
            await self._on_message(d)

    async def _on_message(self, m: dict) -> None:
        author = m.get("author") or {}
        if author.get("bot"):
            return
        content = (m.get("content") or "").strip()
        if not content.startswith(self.prefix):
            return
        # Silently, so there is no "you are not allowed" oracle.
        if str(author.get("id")) != self.owner_id:
            return

        channel_id = m.get("channel_id")
        parts = content[len(self.prefix):].strip().split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]

        try:
            if cmd in ("invite", "gen", "new"):
                await self._cmd_invite(channel_id, args)
            elif cmd in ("invites", "list"):
                await self._cmd_list(channel_id)
            elif cmd in ("revoke", "delete", "del"):
                await self._cmd_revoke(channel_id, args)
            elif cmd in ("help", "commands"):
                await self._reply(channel_id, self._help_text())
            elif cmd == "ping":
                await self._reply(channel_id, "pong 🦇")
            else:
                await self._reply(channel_id, f"Unknown command `{cmd}`. Try `{self.prefix}help`.")
        except Exception as e:  # noqa: BLE001 - never let a handler kill the loop
            logger.exception("Command handler failed")
            await self._reply(channel_id, f"⚠️ Something went wrong: `{e}`")

    async def _cmd_invite(self, channel_id: Optional[str], args: List[str]) -> None:
        count = 1
        if args:
            try:
                count = int(args[0])
            except ValueError:
                await self._reply(channel_id, f"Usage: `{self.prefix}invite [count]` (1 to {MAX_INVITES_PER_COMMAND}).")
                return
        if count < 1 or count > MAX_INVITES_PER_COMMAND:
            await self._reply(channel_id, f"Please ask for between 1 and {MAX_INVITES_PER_COMMAND} invites.")
            return

        codes = await asyncio.to_thread(store.create_invite_tokens, count, self.owner_id)
        listing = "\n".join(f"`{c}`" for c in codes)
        hint = f"\n\nRegister at <{self.frontend_url}> and paste it in the **invite code** field." if self.frontend_url else ""
        noun = "invite" if count == 1 else "invites"
        await self._reply(channel_id, f"✅ Minted {count} one-time {noun}:\n{listing}{hint}")

    async def _cmd_list(self, channel_id: Optional[str]) -> None:
        rows = await asyncio.to_thread(store.list_invite_tokens, False, 25)
        if not rows:
            await self._reply(channel_id, "No outstanding invite tokens. Mint some with "
                                          f"`{self.prefix}invite`.")
            return
        lines = [f"**{len(rows)}** unused invite token(s):"]
        for r in rows:
            created = (r.get("created_at") or "")[:10]
            lines.append(f"`{r['code']}` · created {created}")
        await self._reply(channel_id, "\n".join(lines))

    async def _cmd_revoke(self, channel_id: Optional[str], args: List[str]) -> None:
        if not args:
            await self._reply(channel_id, f"Usage: `{self.prefix}revoke <code>`")
            return
        code = args[0].strip("`").strip()
        ok = await asyncio.to_thread(store.revoke_invite_token, code)
        if ok:
            await self._reply(channel_id, f"🗑️ Revoked invite `{code}`.")
        else:
            await self._reply(channel_id, f"Couldn't revoke `{code}`: unknown or already used.")

    def _help_text(self) -> str:
        p = self.prefix
        return (
            "**Crimson invite bot** (only you can use this)\n"
            f"`{p}invite [n]`: mint *n* one-time invite tokens (1 to {MAX_INVITES_PER_COMMAND}, default 1)\n"
            f"`{p}invites`: list outstanding (unused) tokens\n"
            f"`{p}revoke <code>`: delete an unused token\n"
            f"`{p}ping`: check the bot is alive"
        )

    async def _reply(self, channel_id: Optional[str], content: str) -> None:
        if not channel_id:
            return
        if len(content) > DISCORD_MSG_LIMIT:
            content = content[:DISCORD_MSG_LIMIT - 1] + "…"
        assert self._http is not None, "replies are only sent from inside run()"
        try:
            resp = await self._http.post(
                f"{REST_BASE}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {self.token}"},
                json={"content": content},
            )
            if resp.status_code >= 400:
                logger.warning("Failed to send Discord reply (%s): %s", resp.status_code, resp.text[:200])
        except Exception:  # noqa: BLE001
            logger.exception("Error sending Discord reply")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = get_settings()
    token = settings.discord_bot_token
    owner_id = settings.discord_owner_id
    prefix = settings.discord_command_prefix or "!"

    if not token or not owner_id:
        logger.error(
            "Discord bot disabled: set DISCORD_BOT_TOKEN and DISCORD_OWNER_ID "
            "(in the environment or .env) to enable it."
        )
        return

    bot = InviteBot(token, owner_id, prefix)

    async def _runner() -> None:
        loop = asyncio.get_running_loop()
        # add_signal_handler is POSIX-only; elsewhere Ctrl+C still raises
        # KeyboardInterrupt.
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, bot.stop)
        await bot.run()

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        pass
