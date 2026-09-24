"""What the lifespan starts and drains: schema, background jobs and workers.

Which replica runs which job:

| Jobs | Where | Why |
| --- | --- | --- |
| Retention sweeps | every replica | idempotent DELETEs by timestamp; a second replica costs a no-op query |
| Changelog, IPTV, proxy health | every replica | each replica caches and routes on its own copy |
| Mapping sync, metadata refresh, backfill, airing | the ``RUN_DB_SYNC`` replica | wholesale rebuilds and bulk churn would contend on the shared database |
| Cache, download and music loops | the ``RUN_CACHE_WORKER`` / ``RUN_DOWNLOAD_WORKER`` / ``RUN_MUSIC_WORKER`` service | a long remux or transfer must survive an api redeploy |

Scheduler jobs run in a worker thread with no event loop, so async jobs run on
their own loop there. Warm-ups run once on the main loop at boot, off the boot
path, so an unreachable upstream never delays startup.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from account_engine import audit
from account_engine.db import store as account_store
from apikey_engine.db import store as apikey_store
from cache_engine.db import store as cache_store
from cache_engine.downloader import manager as cache_manager
from changelog_engine.service import service as changelog_service
from chat_engine.db import store as chat_store
from core import config_report, migrations
from core.background import spawn
from core.config import get_settings
from core.db_pool import close_pool
from core.http_client import close_client, open_client
from core.response_cache import purge_expired_cache
from download_engine.db import store as download_store
from download_engine.manager import manager as download_manager
from iptv_engine.service import service as iptv_service
from local_engine.db import store as local_store
from metadata_engine import maintenance, sync_status
from music_engine.worker import worker as music_worker
from metadata_engine.mapping_sync import engine as mapping
from notify_engine import notifier as airing
from notify_engine.db import store as airing_store
from resolvers import _crimson_proxy
from subtitles_engine.service import service as subtitles_service
from supporters_engine.db import store as supporters_store
from system_engine import metrics_state
from telemetry_engine.db import store as telemetry_store


async def start(app: FastAPI, logger: logging.Logger) -> None:
    logger.info("Starting up FastAPI application...")
    # Presence only, never values, so a dark feature is diagnosable from the log.
    config_report.log_report(logger)
    if not get_settings().tmdb_api_key:
        raise RuntimeError("TMDB_API_KEY is not set")
    open_client()
    _init_schema(logger)
    # Here rather than at import, so importing the app never reads the database.
    metrics_state.install()
    _bootstrap_admins(logger)
    app.state.scheduler = _start_scheduler(logger)
    await _start_workers(logger)


async def shutdown(app: FastAPI, logger: logging.Logger) -> None:
    logger.info("Shutting down...")
    await cache_manager.stop()
    await download_manager.stop()
    await music_worker.stop()
    # Waits for a running job, off the loop so the drain can still make progress.
    await asyncio.to_thread(app.state.scheduler.shutdown)
    await close_client()
    close_pool()
    logger.info("Shutdown complete")


def _init_schema(logger: logging.Logger) -> None:
    """The init_db()s own the baseline schema, so they run before the numbered
    migrations. All take the same advisory lock, so concurrent boots serialize."""
    for store in (
        mapping,
        account_store,
        audit,
        apikey_store,
        supporters_store,
        local_store,
        cache_store,
        download_store,
        telemetry_store,
    ):
        store.init_db()
    # Loud in the log and on /health, but a bookkeeping failure must not become a
    # boot loop across every replica.
    try:
        migrations.apply_pending(logger)
    except Exception as e:
        logger.error(f"Schema migrations failed: {e}", exc_info=True)


def _bootstrap_admins(logger: logging.Logger) -> None:
    """Promotes existing ADMIN_EMAILS accounts, so /admin is reachable without
    editing the database."""
    emails = get_settings().admin_emails
    if not emails:
        return
    try:
        promoted = account_store.bootstrap_admins(emails)
        if promoted:
            logger.info(f"Promoted {promoted} account(s) to admin from ADMIN_EMAILS")
    except Exception as e:
        logger.error(f"Admin bootstrap failed: {e}")


async def _start_workers(logger: logging.Logger) -> None:
    """Every queue is a table, so other replicas still queue rows; they just do
    not run the loop."""
    settings = get_settings()
    if settings.run_cache_worker:
        await cache_manager.start_worker()
    else:
        logger.info("RUN_CACHE_WORKER disabled, the cache-worker service downloads")
    if settings.run_download_worker:
        await download_manager.start_worker()
    else:
        logger.info("RUN_DOWNLOAD_WORKER disabled, the download-worker service runs aria2")
    if settings.run_music_worker:
        await music_worker.start()
    else:
        logger.info("RUN_MUSIC_WORKER disabled, the music-worker service downloads")


# --- scheduling helpers ------------------------------------------------------------
def _logged(logger: logging.Logger, label: str, job: Callable[[], object]) -> Callable[[], None]:
    """A job that logs its failure instead of killing the scheduler thread, and
    logs its result when there is one worth reporting."""

    def _run():
        try:
            result = job()
            if result:
                logger.info(f"{label}: {result}")
        except Exception as e:
            logger.error(f"{label} failed: {e}")

    return _run


def _on_own_loop(coro_fn: Callable[[], Coroutine[Any, Any, Any]]) -> Callable[[], object]:
    return lambda: asyncio.run(coro_fn())


def _warm(logger: logging.Logger, label: str, coro_fn: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    async def _run():
        try:
            result = await coro_fn()
            detail = f": {result}" if isinstance(result, (int, str)) else ""
            logger.info(f"{label} warmed{detail}")
        except Exception as e:
            logger.error(f"{label} warm-up failed (will retry on schedule): {e}")

    spawn(_run())


def _start_scheduler(logger: logging.Logger) -> BackgroundScheduler:
    """Called on the event loop thread, because the warm-ups are spawned there."""
    scheduler = BackgroundScheduler()

    def every(job_id: str, label: str, job, **interval) -> None:
        scheduler.add_job(
            _logged(logger, label, job),
            IntervalTrigger(**interval),
            id=job_id,
            replace_existing=True,
        )

    def nightly(job_id: str, label: str, job, hour: int) -> None:
        scheduler.add_job(
            _logged(logger, label, job),
            CronTrigger(hour=hour, minute=0),
            id=job_id,
            replace_existing=True,
        )

    _every_replica(every)
    _cached_services(logger, every)
    _sync_replica(logger, every, nightly)
    scheduler.start()
    logger.info("Background scheduler started")
    return scheduler


# --- the jobs ----------------------------------------------------------------------
def _every_replica(every) -> None:
    def _purge():
        # Challenges requested and never completed would otherwise pile up, and
        # every unique search writes an api_cache row.
        account_store.purge_expired()
        return {
            "api_cache": purge_expired_cache(),
            "security_events": audit.purge_old(),
            "airing": airing_store.purge_old(),
            "telemetry": telemetry_store.purge_old(),
        }

    every("purge_expired_job", "Retention sweep", _purge, hours=6)
    every("chat_prune_job", "Chat prune", chat_store.prune, hours=12)


def _cached_services(logger, every) -> None:
    settings = get_settings()

    # ETag requests keep the refresh near-free against GitHub's rate limit.
    if changelog_service.configured():
        _warm(logger, "Changelog", lambda: asyncio.to_thread(changelog_service.refresh))
        every("changelog_refresh_job", "Changelog refresh", changelog_service.refresh, minutes=30)
    else:
        logger.info("GITHUB_TOKEN not set, /changelog will return 503 until configured")

    # About 25 MB of JSON, published daily upstream.
    if settings.iptv_enabled:
        _warm(logger, "IPTV catalogue", lambda: asyncio.to_thread(iptv_service.refresh))
        every("iptv_refresh_job", "IPTV catalogue refresh", iptv_service.refresh, hours=12)
    else:
        logger.info("IPTV_ENABLED=false, the Live TV surface is dark")

    # Lets proxy_url route only to hosts that are up: failover between deploys.
    if _crimson_proxy.is_enabled():
        _warm(logger, "Proxy health", _crimson_proxy.refresh_health)
        every(
            "proxy_health_job",
            "Proxy health refresh",
            _on_own_loop(_crimson_proxy.refresh_health),
            minutes=2,
        )
    else:
        logger.info("CRIMSON_PROXY_BASE not set, external CORS proxy disabled, /sign returns 503")

    if subtitles_service.configured():
        logger.info("OpenSubtitles configured, /subtitles is enabled")
    else:
        logger.info("OPENSUBTITLES_API_KEY not set, /subtitles will return 503 until configured")


async def _initial_sync(logger: logging.Logger) -> None:
    """Off the boot path, so /health comes up at once instead of after a
    multi-minute download. A warm database pays only the conditional HEAD."""
    sync_status.set_phase("running", "Fribb mapping sync started", started=True)
    try:
        # On its own thread and loop, so the heavy writes never stall requests.
        result = await asyncio.to_thread(asyncio.run, mapping.sync_database_async())
    except Exception as e:
        sync_status.set_phase("failed", str(e), finished=True)
        logger.error(f"Initial database sync failed: {e}")
        return
    phases = {
        "up_to_date": ("up_to_date", "Mappings already up-to-date"),
        "synced": ("done", "Mapping tables rebuilt from Fribb"),
    }
    phase, message = phases.get(result, ("failed", result or "unknown outcome"))
    sync_status.set_phase(phase, message, finished=True)
    logger.info(f"Initial mapping sync: {result}")


def _sync_replica(logger, every, nightly) -> None:
    settings = get_settings()
    if settings.demo_mode:
        logger.warning(
            "DEMO_MODE is ON: signup invite gate is bypassed, non-admin data resets "
            f"nightly at {settings.demo_reset_hour:02d}:00 (server time)"
        )
    if not settings.run_db_sync:
        logger.info("RUN_DB_SYNC is disabled, this replica will not run the mapping resync")
        sync_status.set_phase("disabled", "RUN_DB_SYNC is off on this replica")
        return

    if settings.demo_mode:
        # Signup is open, so all non-admin data is wiped nightly to bound growth.
        nightly(
            "demo_reset_job",
            "DEMO_MODE nightly reset",
            account_store.wipe_demo_data,
            settings.demo_reset_hour,
        )

    spawn(_initial_sync(logger))
    every(
        "db_sync_job", "Scheduled mapping sync", _on_own_loop(mapping.sync_database_async), hours=24
    )
    # Nothing upstream reports a TMDB change, so the tables are swept oldest-first.
    nightly(
        "metadata_nightly_refresh_job",
        "Nightly metadata refresh",
        _on_own_loop(maintenance.refresh_daily_slice),
        settings.metadata_refresh_hour,
    )
    # A dashboard backfill arrives through a table, because a serving replica
    # cannot reach this portless container. APScheduler's default max_instances=1
    # keeps a long run from stacking ticks.
    every(
        "metadata_backfill_drain_job",
        "Backfill drain",
        _on_own_loop(maintenance.run_pending_backfill),
        minutes=1,
    )
    if settings.run_metadata_backfill:
        _warm(logger, "Startup metadata backfill", maintenance.backfill_catalogue)

    # A fresh deploy would otherwise show an empty calendar until the first tick.
    # Six-hourly: a slipped broadcast is the only thing that changes.
    _warm(logger, "Airing schedule", airing.refresh_schedule)
    every(
        "airing_refresh_job",
        "Airing schedule refresh",
        _on_own_loop(airing.refresh_schedule),
        hours=6,
    )

    if not settings.airing_notify_enabled:
        logger.info(
            "AIRING_NOTIFY_ENABLED is off, the calendar and follows work but nobody is mailed"
        )
        return
    if settings.airing_notify_dry_run:
        logger.warning(
            "AIRING_NOTIFY_DRY_RUN is ON: notifications are claimed and logged, but nobody is mailed"
        )

    def _notify():
        result = airing.send_due_notifications()
        return result if result["claimed"] else None

    # Close behind the broadcast; it touches only the database until there is mail.
    every("airing_notify_job", "Airing notifications", _notify, minutes=10)
