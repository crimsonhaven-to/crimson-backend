"""
Startup configuration report.

Crimson has a lot of *optional*, env-gated features (Jellyfin, Febbox, the
external proxy offload, OpenSubtitles, SMTP, the Discord bot, Ko-fi supporters,
…). When one is "dark" it's almost always a missing/blank env var, and there was
no single place to see what's on. ``build_report()`` inspects the environment and
returns a tidy, **secret-free** summary (presence only, never values) that
``log_report()`` prints once at startup, e.g.::

    Crimson feature configuration:
      [ on] Jellyfin personal source
      [off] OpenSubtitles subtitles            — set OPENSUBTITLES_API_KEY
      [WARN] Proxy signing secret               — PROXY_SECRET unset: signed
             proxies use a random per-process secret (breaks across replicas)

This is diagnostics only; it never raises (hard requirements stay in
``Config.validate()``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional


def _has(*names: str) -> bool:
    """True if every named env var is set and non-empty."""
    return all((os.getenv(n) or "").strip() for n in names)


def _flag_on(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no")


@dataclass
class Feature:
    label: str
    # True => enabled, False => disabled, None => warning (enabled but risky).
    state: Callable[[], Optional[bool]]
    # Shown when disabled (how to turn it on) or warning (what's wrong).
    hint: str = ""


def _proxy_secret_state() -> Optional[bool]:
    # Enabled+safe when a stable shared secret is set; warn when it isn't (a
    # random per-process secret breaks signed-link verification across replicas).
    return True if _has("PROXY_SECRET") else None


FEATURES: List[Feature] = [
    Feature("TMDB metadata (required)", lambda: _has("TMDB_API_KEY"),
            "set TMDB_API_KEY — the app will not start without it"),
    Feature("Login wall (members-only)", lambda: _flag_on("REQUIRE_LOGIN", True),
            "REQUIRE_LOGIN=false serves a fully open API"),
    Feature("Proxy signing secret", _proxy_secret_state,
            "PROXY_SECRET unset: signed proxies use a random per-process secret "
            "(breaks across replicas + on restart)"),
    Feature("External proxy offload", lambda: _has("CRIMSON_PROXY_BASE", "PROXY_SECRET"),
            "set CRIMSON_PROXY_BASE (+ PROXY_SECRET) to offload HLS segments"),
    Feature("Jellyfin personal source", lambda: _has("JELLYFIN_URL"),
            "set JELLYFIN_URL (+ JELLYFIN_USERNAME/PASSWORD)"),
    Feature("Jellyfin edge token-inject", lambda: _flag_on("JELLYFIN_EDGE_INJECT", False),
            "JELLYFIN_EDGE_INJECT=true moves token injection to the edge proxy"),
    Feature("ShowBox/Febbox source", lambda: _has("FEBBOX_UI_TOKEN"),
            "set FEBBOX_UI_TOKEN to enable the direct-file source"),
    Feature("OpenSubtitles subtitles", lambda: _has("OPENSUBTITLES_API_KEY"),
            "set OPENSUBTITLES_API_KEY"),
    Feature("Transactional email (SMTP)", lambda: _has("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"),
            "set SMTP_HOST/SMTP_USER/SMTP_PASSWORD for verify + reset mail"),
    Feature("Changelog (GitHub Releases)", lambda: _has("GITHUB_TOKEN"),
            "set GITHUB_TOKEN to expose /changelog (503 otherwise)"),
    Feature("Discord invite bot", lambda: _has("DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID"),
            "set DISCORD_BOT_TOKEN + DISCORD_OWNER_ID"),
    Feature("Ko-fi supporters webhook", lambda: _has("KOFI_VERIFICATION_TOKEN"),
            "set KOFI_VERIFICATION_TOKEN to ingest Ko-fi events"),
    Feature("Invite-gated signup", lambda: _has("SIGNUP_INVITE_CODE"),
            "set SIGNUP_INVITE_CODE for a reusable invite (bot mints single-use)"),
    Feature("Admin seed", lambda: _has("ADMIN_EMAILS"),
            "set ADMIN_EMAILS (comma-separated) to seed the first admin"),
    Feature("Fribb mapping resync (this replica)", lambda: _flag_on("RUN_DB_SYNC", True),
            "RUN_DB_SYNC=false on serving replicas; true on exactly one"),
    Feature("Server-side cache worker (this replica)", lambda: _flag_on("RUN_CACHE_WORKER", True),
            "RUN_CACHE_WORKER true only on the cache-worker service"),
]


def build_report() -> List[str]:
    """Return the report as a list of pre-formatted lines (no I/O, easy to test)."""
    lines: List[str] = ["Crimson feature configuration:"]
    for feat in FEATURES:
        try:
            state = feat.state()
        except Exception:
            state = None
        if state is True:
            tag = " on"
            suffix = ""
        elif state is False:
            tag = "off"
            suffix = f"  - {feat.hint}" if feat.hint else ""
        else:  # None => warning
            tag = "WARN"
            suffix = f"  - {feat.hint}" if feat.hint else ""
        lines.append(f"  [{tag:>4}] {feat.label}{suffix}")
    return lines


def log_report(logger: Optional[logging.Logger] = None) -> None:
    """Log the startup feature report. Never raises."""
    log = logger or logging.getLogger("crimson.config")
    try:
        for line in build_report():
            log.info(line)
    except Exception as e:  # diagnostics must never break startup
        log.warning("config report failed: %s", e)
