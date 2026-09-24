"""The feature report logged once at startup.

Most features are env-gated and a dark one is almost always a missing env var,
so the boot log lists each feature as on, off or WARN with the fix. It reports
presence only, never values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from core.config import Settings, get_settings


@dataclass
class Feature:
    label: str
    # True enabled, False disabled, None warning (on but risky).
    state: Callable[[Settings], Optional[bool]]
    # How to turn it on, or what is wrong when warning.
    hint: str = ""


def _proxy_secret_state(s: Settings) -> Optional[bool]:
    # Warn without a stable shared secret: a random per-process one breaks
    # signed-link verification across replicas.
    return True if s.proxy_secret else None


def _airing_notify_state(s: Settings) -> Optional[bool]:
    # A dry run and a live run both read as "enabled" in the environment, and
    # the boot log is the only place the difference shows, so a dry run warns.
    if not s.airing_notify_enabled:
        return False
    if s.airing_notify_dry_run or not s.smtp_host:
        return None
    return True


FEATURES: List[Feature] = [
    Feature("TMDB metadata (required)", lambda s: bool(s.tmdb_api_key),
            "set TMDB_API_KEY; the app will not start without it"),
    Feature("Login wall (members-only)", lambda s: s.require_login,
            "REQUIRE_LOGIN=false serves a fully open API"),
    Feature("Proxy signing secret", _proxy_secret_state,
            "PROXY_SECRET unset: signed proxies use a random per-process secret "
            "(breaks across replicas + on restart)"),
    Feature("External proxy offload", lambda s: bool(s.crimson_proxy_base and s.proxy_secret),
            "set CRIMSON_PROXY_BASE (+ PROXY_SECRET) to offload HLS segments"),
    Feature("Jellyfin personal source", lambda s: bool(s.jellyfin_url),
            "set JELLYFIN_URL (+ JELLYFIN_USERNAME/PASSWORD)"),
    Feature("Jellyfin edge token-inject", lambda s: s.jellyfin_edge_inject,
            "JELLYFIN_EDGE_INJECT=true moves token injection to the edge proxy"),
    Feature("OpenSubtitles subtitles", lambda s: bool(s.opensubtitles_api_key),
            "set OPENSUBTITLES_API_KEY"),
    Feature("Manga reading surface", lambda s: s.manga_enabled,
            "MANGA_ENABLED=false disables the manga surface (pages resolve client-side; "
            "a server-side provider is optional via the source overlay)"),
    Feature("Live TV surface (iptv-org)", lambda s: s.iptv_enabled,
            "IPTV_ENABLED=false disables the Live TV catalogue + /iptv_proxy"),
    Feature("Transactional email (SMTP)",
            lambda s: bool(s.smtp_host and s.smtp_user and s.smtp_password),
            "set SMTP_HOST/SMTP_USER/SMTP_PASSWORD for verify + reset mail"),
    Feature("Changelog (GitHub Releases)", lambda s: bool(s.github_token),
            "set GITHUB_TOKEN to expose /changelog (503 otherwise)"),
    Feature("Airing email notifications", _airing_notify_state,
            "set AIRING_NOTIFY_ENABLED=true (and SMTP_*) to mail subscribers when "
            "an episode airs; the calendar and follows work either way"),
    Feature("Discord invite bot", lambda s: bool(s.discord_bot_token and s.discord_owner_id),
            "set DISCORD_BOT_TOKEN + DISCORD_OWNER_ID"),
    Feature("Ko-fi supporters webhook", lambda s: bool(s.kofi_verification_token),
            "set KOFI_VERIFICATION_TOKEN to ingest Ko-fi events"),
    # A key alone does not mean the chatbot is live: whether it is on and who may
    # use it are operator settings in the database.
    Feature("Lumi chatbot key", lambda s: bool(s.anthropic_api_key or s.gemini_api_key),
            "set ANTHROPIC_API_KEY or GEMINI_API_KEY, then switch Lumi on in "
            "Admin -> Lumi and grant members access in Admin -> Users"),
    Feature("Music library", lambda s: bool(s.music_root),
            "set MUSIC_ROOT to the music share's mount, then grant members access "
            "in Admin -> Users"),
    Feature("Music CDN copy", lambda s: bool(s.music_cdn_url and s.music_cdn_secret),
            "set MUSIC_CDN_URL + MUSIC_CDN_SECRET to copy the library to the music-cdn "
            "Worker's R2 bucket and stream from there (see deploy/music-cdn)"),
    Feature("Invite-gated signup", lambda s: bool(s.signup_invite_code),
            "set SIGNUP_INVITE_CODE for a reusable invite (bot mints single-use)"),
    Feature("Admin seed", lambda s: bool(s.admin_emails),
            "set ADMIN_EMAILS (comma-separated) to seed the first admin"),
    Feature("Metrics scrape token", lambda s: bool(s.metrics_token),
            "set METRICS_TOKEN for a Prometheus scrape; without it /metrics is "
            "reachable only with an admin session"),
    Feature("Metrics history (Prometheus)", lambda s: bool(s.prometheus_url),
            "set PROMETHEUS_URL (e.g. http://prometheus:9090) to give the admin "
            "dashboard charts over time; unset leaves it on the live snapshot"),
    Feature("JSON log format", lambda s: s.log_format == "json",
            "LOG_FORMAT=json emits one JSON object per line (default is the plain format)"),
    Feature("Fribb mapping resync (this replica)", lambda s: s.run_db_sync,
            "RUN_DB_SYNC=false on serving replicas; true on exactly one"),
    Feature("Server-side cache worker (this replica)", lambda s: s.run_cache_worker,
            "RUN_CACHE_WORKER true only on the cache-worker service"),
    Feature("Music download worker (this replica)", lambda s: s.run_music_worker,
            "RUN_MUSIC_WORKER true only on the music-worker service"),
]


def _grant_state(probe: Callable[[], bool]) -> Callable[[Settings], Optional[bool]]:
    return lambda _settings: probe()


def _overlay_features() -> List[Feature]:
    """Lines declared by overlay sources as ``config_feature`` on their
    RESOLVE_GRANT, so the public config never names an overlay source."""
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
            feats.append(Feature(label, _grant_state(probe), hint))
    return feats


def build_report() -> List[str]:
    settings = get_settings()
    lines: List[str] = ["Crimson feature configuration:"]
    for feat in FEATURES + _overlay_features():
        try:
            state = feat.state(settings)
        except Exception:
            state = None
        tag = " on" if state is True else "off" if state is False else "WARN"
        suffix = f"  - {feat.hint}" if feat.hint and state is not True else ""
        lines.append(f"  [{tag:>4}] {feat.label}{suffix}")
    return lines


def log_report(logger: Optional[logging.Logger] = None) -> None:
    log = logger or logging.getLogger("crimson.config")
    try:
        for line in build_report():
            log.info(line)
    except Exception as e:  # Diagnostics must never break startup.
        log.warning("config report failed: %s", e)
