"""
Discord invite bot for the Crimsonhaven backend.

A tiny, dependency-light Discord *gateway* bot whose only job is to let ONE
whitelisted operator mint single-use invite tokens for the (otherwise invite-only)
signup flow. It speaks the Discord gateway protocol directly over a WebSocket
(``websockets``) and replies via the REST API (``httpx``) — no discord.py — to
stay friendly to the python:3.14-slim deploy image (no heavy/uncertain native
deps), in the same spirit as the vendored Ed25519 in account_engine.

Configuration (environment / .env):
    DISCORD_BOT_TOKEN   the bot token (Discord Developer Portal -> Bot)
    DISCORD_OWNER_ID    the numeric user id allowed to use the bot; everyone
                        else is ignored
    DISCORD_COMMAND_PREFIX  optional, defaults to "!"

Run it as its own process (it must be a SINGLE instance — a second gateway
connection with the same token fights the first and double-mints):

    python -m discord_bot

Tokens are created via the shared account store (account_engine.db.AccountStore),
so they land in the same PostgreSQL the API reads at /auth/email/register.
"""

from .bot import InviteBot, main

__all__ = ["InviteBot", "main"]
