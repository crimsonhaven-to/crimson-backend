"""
Startup configuration report.

A lot of features here are optional and env-gated, and a dark one is almost
always a missing env var. ``build_report()`` inspects the environment and returns
a secret-free summary, presence only and never values, which ``log_report()``
prints once at startup::

    Crimson feature configuration:
      [ on] Jellyfin personal source
      [off] OpenSubtitles subtitles     - set OPENSUBTITLES_API_KEY

Diagnostics only, and never raises. Hard requirements stay in ``Config.validate()``.
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
    # True enabled, False disabled, None warning (on but risky).
    state: Callable[[], Optional[bool]]
    # How to turn it on, or what is wrong when warning.
    hint: str = ""


def _proxy_secret_state() -> Optional[bool]:
    # Warn without a stable shared secret: a random per-process one breaks
    # signed-link verification across replicas.
    return True if _has("PROXY_SECRET") else None


def _prometheus_state() -> Optional[bool]:
    """Whether the optional prometheus-client dependency is in this build.
    Imported lazily so diagnostics never drag metrics into an import cycle."""
    try:
        from core.observability import PROMETHEUS_AVAILABLE
        return PROMETHEUS_AVAILABLE
    except Exception:
        return False


FEATURES: List[Feature] = [
    Feature("TMDB metadata (required)", lambda: _has("TMDB_API_KEY"),
            "set TMDB_API_KEY; the app will not start without it"),
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
    Feature("OpenSubtitles subtitles", lambda: _has("OPENSUBTITLES_API_KEY"),
            "set OPENSUBTITLES_API_KEY"),
    Feature("Manga reading surface", lambda: _flag_on("MANGA_ENABLED", True),
            "MANGA_ENABLED=false disables the manga surface (pages resolve client-side; "
            "a server-side provider is optional via the source overlay)"),
    Feature("Live TV surface (iptv-org)", lambda: _flag_on("IPTV_ENABLED", True),
            "IPTV_ENABLED=false disables the Live TV catalogue + /iptv_proxy"),
    Feature("Transactional email (SMTP)", lambda: _has("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"),
            "set SMTP_HOST/SMTP_USER/SMTP_PASSWORD for verify + reset mail"),
    Feature("Changelog (GitHub Releases)", lambda: _has("GITHUB_TOKEN"),
            "set GITHUB_TOKEN to expose /changelog (503 otherwise)"),
    Feature("Discord invite bot", lambda: _has("DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID"),
            "set DISCORD_BOT_TOKEN + DISCORD_OWNER_ID"),
    Feature("Ko-fi supporters webhook", lambda: _has("KOFI_VERIFICATION_TOKEN"),
            "set KOFI_VERIFICATION_TOKEN to ingest Ko-fi events"),
    # Only the key needs an env var. Whether Lumi is awake, which provider answers
    # and who may talk to her are operator settings in the database, so a key alone
    # does not mean the feature is live.
    Feature("Lumi chatbot key", lambda: _has("ANTHROPIC_API_KEY") or _has("GEMINI_API_KEY"),
            "set ANTHROPIC_API_KEY or GEMINI_API_KEY, then switch Lumi on in "
            "Admin -> Lumi and grant members access in Admin -> Users"),
    Feature("Invite-gated signup", lambda: _has("SIGNUP_INVITE_CODE"),
            "set SIGNUP_INVITE_CODE for a reusable invite (bot mints single-use)"),
    Feature("Admin seed", lambda: _has("ADMIN_EMAILS"),
            "set ADMIN_EMAILS (comma-separated) to seed the first admin"),
    # An optional import, so this reports a property of the build rather than the
    # environment: the only place a stripped image announces /metrics will 503.
    Feature("Prometheus metrics support", _prometheus_state,
            "prometheus-client is not installed in this build; /metrics answers 503"),
    Feature("Metrics scrape token", lambda: _has("METRICS_TOKEN"),
            "set METRICS_TOKEN for a Prometheus scrape; without it /metrics is "
            "reachable only with an admin session"),
    # Without this the Metrics tab still works, just with no time axis.
    Feature("Metrics history (Prometheus)", lambda: _has("PROMETHEUS_URL"),
            "set PROMETHEUS_URL (e.g. http://prometheus:9090) to give the admin "
            "dashboard charts over time; unset leaves it on the live snapshot"),
    Feature("JSON log format", lambda: os.getenv("LOG_FORMAT", "plain").strip().lower() == "json",
            "LOG_FORMAT=json emits one JSON object per line (default is the plain format)"),
    Feature("Fribb mapping resync (this replica)", lambda: _flag_on("RUN_DB_SYNC", True),
            "RUN_DB_SYNC=false on serving replicas; true on exactly one"),
    Feature("Server-side cache worker (this replica)", lambda: _flag_on("RUN_CACHE_WORKER", True),
            "RUN_CACHE_WORKER true only on the cache-worker service"),
]


def _overlay_features() -> List[Feature]:
    """Feature lines contributed by the build-time source overlay, empty in a base
    build. An overlay source declares a ``config_feature`` on its RESOLVE_GRANT, so
    surfacing them here reports the operator build without the public config
    naming any overlay source."""
    try:
        import resolvers as _resolvers_pkg
        from core.private_sources import discover_resolve_grants
    except Exception:
        return []
    feats: List[Feature] = []
    for desc in discover_resolve_grants(_resolvers_pkg):
        cf = desc.get("config_feature")
        probe = desc.get("is_configured")
        if cf and probe:
            label, hint = cf
            feats.append(Feature(label, probe, hint))
    return feats


def build_report() -> List[str]:
    """The report as pre-formatted lines. No I/O, so it is easy to test."""
    lines: List[str] = ["Crimson feature configuration:"]
    for feat in FEATURES + _overlay_features():
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
        else:  # None means warning
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
