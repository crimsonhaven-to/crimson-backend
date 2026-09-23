"""
Lifespan: schema, background jobs and workers.

Everything ``api.py``'s ``lifespan`` used to hold inline. It lived there as one
360-line function, which buried the single most load-bearing fact about this
process: **which replica runs which job.**

Three rules decide that, and each has its own registration function below:

| Rule | Function | Why |
| --- | --- | --- |
| Every replica | :func:`_register_every_replica_jobs` | Idempotent `DELETE`s by timestamp, so extra replicas cost a redundant no-op query rather than correctness |
| Every replica, per service | :func:`_register_optional_service_jobs` | Each replica keeps its own cache and routes independently, so each refreshes its own |
| The `RUN_DB_SYNC` replica only | :func:`_register_sync_replica_jobs` | Wholesale table rebuilds and bulk metadata churn: several replicas doing it in lockstep wastes bandwidth and contends on the shared DB |

This module sits beside ``api.py`` rather than under ``core/`` on purpose. It
imports downward into ``core``, the engines and ``web``, which is the direction
imports are supposed to flow; ``core`` is the layer everything else imports, and
the one place it needs engine state (``core/observability.py``) it reaches for it
with a deferred, function-local import to keep that property.

The ``logger`` every function takes is ``api.py``'s, so the startup log reads as
one unbroken sequence from one source, exactly as it did inline. That is the same
thing ``migrations.apply_pending(logger)`` already does.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from core import config_report, migrations, observability
from core.config import Config
from core.db_pool import close_pool
from core.http_client import close_client as close_http_client
from core.response_cache import purge_expired_cache
from core.background import spawn
from resolvers import _crimson_proxy

from account_engine import audit as security_audit
from account_engine import store as account_store
from apikey_engine import store as apikey_store
from cache_engine.downloader import manager as cache_manager
from changelog_engine import service as changelog_service
from chat_engine import store as chat_store
from download_engine.manager import manager as download_manager
from iptv_engine import enabled as iptv_enabled
from iptv_engine import service as iptv_service
from metadata_engine import maintenance as metadata_maintenance
from metadata_engine import sync_status
from notify_engine import notifier as airing_notifier
from notify_engine import store as airing_store
from subtitles_engine import service as subtitles_service
from supporters_engine import store as supporters_store

from web.context import (
    cache_store,
    db_engine,
    download_store,
    local_source_store,
    telemetry_store,
)


def report_config(logger: logging.Logger) -> None:
    """Log which features are configured.

    Presence only, never values, so a dark source is diagnosable from the boot
    log at a glance."""
    config_report.log_report(logger)


def init_schema(logger: logging.Logger) -> None:
    """Create every store's schema, then apply the numbered migrations.

    Order is load-bearing: the ``init_db()`` functions still own the
    pre-migration baseline (see ``core/migrations.py``), so they run first and
    the runner owns version 0 onward."""
    # Idempotent, so safe on every replica.
    db_engine.init_db()
    # None of these tables are touched by a mapping resync.
    account_store.init_db()
    security_audit.init_db()
    apikey_store.init_db()
    supporters_store.init_db()
    local_source_store.init_db()
    cache_store.init_db()
    download_store.init_db()
    telemetry_store.init_db()

    # Takes the same advisory lock as the init_db()s, so concurrent boots
    # serialize. Non-fatal by design: a failure is loud in the log and on
    # /health, but must not turn a bookkeeping problem into a boot loop across
    # every replica.
    try:
        migrations.apply_pending(logger)
    except Exception as e:
        logger.error(f"Schema migrations failed: {e}", exc_info=True)


def install_observability(logger: logging.Logger) -> None:
    """Wire the scrape-time metric collector.

    Called from lifespan rather than at import, so merely importing the app (a
    test, the openapi export) never wires up something that reads the DB."""
    observability.install_state_collector()
    if not observability.PROMETHEUS_AVAILABLE:
        logger.info("prometheus_client not installed; /metrics is inert (503)")


def bootstrap_admins(logger: logging.Logger) -> None:
    """Promote the ADMIN_EMAILS accounts.

    Idempotent, and only promotes accounts that already exist, so the operator
    reaches /admin without hand-editing the DB."""
    if not Config.ADMIN_EMAILS:
        return
    try:
        promoted = account_store.bootstrap_admins(Config.ADMIN_EMAILS)
        if promoted:
            logger.info(f"Promoted {promoted} account(s) to admin from ADMIN_EMAILS")
    except Exception as e:
        logger.error(f"Admin bootstrap failed: {e}")


def start_scheduler(logger: logging.Logger) -> BackgroundScheduler:
    """Register every background job and start the scheduler.

    **Must be called from the event loop thread.** The warm-ups registered here
    are fired with ``asyncio.create_task``, which needs a running loop in the
    calling thread. Each warm-up stays paired with the refresh job it warms, and
    behind the same feature guard, so the two can never drift apart.
    """
    # One per replica. It always owns the cheap housekeeping; the heavy Fribb
    # resync is added on exactly one replica.
    scheduler = BackgroundScheduler()

    _register_every_replica_jobs(scheduler, logger)
    _register_optional_service_jobs(scheduler, logger)
    _register_sync_replica_jobs(scheduler, logger)

    scheduler.start()
    logger.info("Background scheduler started")
    return scheduler


# --- every replica ----------------------------------------------------------
# Retention sweeps. Each is an idempotent DELETE by timestamp, so several
# replicas sweeping on their own clocks costs a redundant no-op query rather than
# correctness, and none of them needs pinning.

def _register_every_replica_jobs(scheduler: BackgroundScheduler, logger: logging.Logger) -> None:
    # Rows are already deleted on access, but a challenge that is requested and
    # never completed would pile up until the next restart.
    def _purge_expired():
        try:
            account_store.purge_expired()
        except Exception as e:
            logger.error(f"Expired session/challenge purge failed: {e}")
        # Consume-on-read never deletes these, and every unique search query
        # writes one, so the table would grow unbounded.
        try:
            n = purge_expired_cache()
            if n:
                logger.info(f"Purged {n} expired api_cache rows")
        except Exception as e:
            logger.error(f"Expired api_cache purge failed: {e}")
        # An idempotent DELETE, so several replicas sweeping on their own clocks
        # is fine.
        try:
            n = security_audit.purge_old()
            if n:
                logger.info(f"Purged {n} security events past retention")
        except Exception as e:
            logger.error(f"Security event purge failed: {e}")
        # Same shape again: old airings, and notification ledger rows old enough
        # that the episodes they name are long gone from the schedule.
        try:
            removed = airing_store.purge_old()
            if removed["schedule"] or removed["notifications"]:
                logger.info(
                    f"Airing prune: {removed['schedule']} schedule row(s), "
                    f"{removed['notifications']} notification row(s)"
                )
        except Exception as e:
            logger.error(f"Airing prune failed: {e}")
        try:
            n = telemetry_store.purge_old()
            if n:
                logger.info(f"Purged {n} resolve telemetry rows")
        except Exception as e:
            logger.error(f"Telemetry purge failed: {e}")

    scheduler.add_job(
        _purge_expired,
        trigger=IntervalTrigger(hours=6),
        id="purge_expired_job",
        replace_existing=True,
    )

    def _prune_chat():
        try:
            removed = chat_store.prune()
            if removed["conversations"] or removed["usage"]:
                logger.info(
                    f"Chat prune: {removed['conversations']} conversation(s), "
                    f"{removed['usage']} usage row(s)"
                )
        except Exception as e:
            logger.error(f"Chat prune failed: {e}")

    scheduler.add_job(
        _prune_chat,
        trigger=IntervalTrigger(hours=12),
        id="chat_prune_job",
        replace_existing=True,
    )


# --- every replica, one per optional service --------------------------------
# Each of these caches something in-process, so every replica keeps and refreshes
# its own copy. The initial warm-up is fired off the boot path with
# ``asyncio.create_task`` so an unreachable upstream never delays startup.

def _register_optional_service_jobs(scheduler: BackgroundScheduler, logger: logging.Logger) -> None:
    # ETag conditional requests keep the refresh near-free against GitHub's rate
    # limit. The warm-up runs off the event loop.
    if changelog_service.configured():
        async def _warm_changelog():
            try:
                await run_in_threadpool(changelog_service.refresh)
                logger.info("Changelog cache warmed from GitHub Releases")
            except Exception as e:
                logger.error(f"Initial changelog warm-up failed (will retry on schedule): {e}")

        spawn(_warm_changelog())  # fire-and-forget

        def _refresh_changelog():
            try:
                changelog_service.refresh()
            except Exception as e:
                logger.error(f"Changelog refresh failed: {e}")

        scheduler.add_job(
            _refresh_changelog,
            trigger=IntervalTrigger(minutes=30),
            id="changelog_refresh_job",
            replace_existing=True,
        )
    else:
        logger.info("GITHUB_TOKEN not set, /changelog will return 503 until configured")

    # The warm-up is a ~25 MB JSON pull, so it runs off the event loop; upstream
    # publishes daily, so the refresh interval matches. Routes also self-heal by
    # kicking a refresh when asked while stale.
    if iptv_enabled():
        async def _warm_iptv():
            try:
                await run_in_threadpool(iptv_service.refresh)
                logger.info("IPTV catalogue warmed from iptv-org")
            except Exception as e:
                logger.error(f"Initial IPTV warm-up failed (will retry on schedule): {e}")

        spawn(_warm_iptv())  # fire-and-forget

        def _refresh_iptv():
            try:
                iptv_service.refresh()
            except Exception as e:
                logger.error(f"IPTV catalogue refresh failed: {e}")

        scheduler.add_job(
            _refresh_iptv,
            trigger=IntervalTrigger(hours=12),
            id="iptv_refresh_job",
            replace_existing=True,
        )
    else:
        logger.info("IPTV_ENABLED=false, the Live TV surface is dark")

    # Probing every host lets proxy_url route only to the ones that are up,
    # giving automatic failover between the deploys. A couple of GETs per host,
    # when configured.
    if _crimson_proxy.is_enabled():
        async def _warm_proxy_health():
            try:
                await _crimson_proxy.refresh_health()
                logger.info("CORS proxy health cache warmed")
            except Exception as e:
                logger.error(f"Initial proxy health probe failed (will retry on schedule): {e}")

        spawn(_warm_proxy_health())  # must not delay startup

        # BackgroundScheduler runs jobs in a worker thread with no running event
        # loop, so the job spins up its own.
        def _refresh_proxy_health():
            try:
                asyncio.run(_crimson_proxy.refresh_health())
            except Exception as e:
                logger.error(f"Proxy health refresh failed: {e}")

        scheduler.add_job(
            _refresh_proxy_health,
            trigger=IntervalTrigger(minutes=2),
            id="proxy_health_job",
            replace_existing=True,
        )
    else:
        logger.info("CRIMSON_PROXY_BASE not set, external CORS proxy disabled, /sign returns 503")

    # No job, no cache: the route calls OpenSubtitles per request. Reported here
    # so the boot log accounts for every optional surface.
    if subtitles_service.configured():
        logger.info("OpenSubtitles configured, /subtitles is enabled")
    else:
        logger.info("OPENSUBTITLES_API_KEY not set, /subtitles will return 503 until configured")


# --- the RUN_DB_SYNC replica only -------------------------------------------
# Wholesale rebuilds and bulk metadata churn. Every replica doing this in
# lockstep would waste the bandwidth N times over and contend on the shared DB,
# so exactly one container owns it.

def _register_sync_replica_jobs(scheduler: BackgroundScheduler, logger: logging.Logger) -> None:
    # Signup is open in demo mode, so all non-admin data is wiped nightly to
    # bound growth. Pinned so replicas don't race the DELETE.
    if Config.DEMO_MODE:
        logger.warning(
            "DEMO_MODE is ON: signup invite gate is bypassed, non-admin data resets "
            f"nightly at {Config.DEMO_RESET_HOUR:02d}:00 (server time)"
        )
        if Config.RUN_DB_SYNC:
            def _demo_reset():
                try:
                    res = account_store.wipe_demo_data()
                    logger.info(f"DEMO_MODE nightly reset done: {res}")
                except Exception as e:
                    logger.error(f"DEMO_MODE nightly reset failed: {e}")

            scheduler.add_job(
                _demo_reset,
                trigger=CronTrigger(hour=Config.DEMO_RESET_HOUR, minute=0),
                id="demo_reset_job",
                replace_existing=True,
            )
        else:
            logger.info("DEMO_MODE: this replica is not RUN_DB_SYNC, the nightly reset runs on the sync replica")

    if not Config.RUN_DB_SYNC:
        logger.info("RUN_DB_SYNC is disabled, this replica will not run the mapping resync")
        sync_status.set_phase("disabled", "RUN_DB_SYNC is off on this replica")
        return

    # Fire-and-forget, so uvicorn and /health come up immediately rather than
    # blocking boot on a multi-minute download and enrichment. That matters most
    # in single-replica dev, where the one container is also the sync replica.
    # sync_database_async HEADs the Fribb URL first and returns "up_to_date" when
    # the stored ETag still matches a non-empty DB, so a warm DB pays only that
    # conditional HEAD.
    #
    # Pushed onto a worker thread, the same shape the scheduled job uses, so the
    # heavy synchronous writes never stall the loop now serving requests.
    async def _initial_sync():
        sync_status.set_phase("running", "Fribb mapping sync started", started=True)
        try:
            result = await run_in_threadpool(
                lambda: asyncio.run(db_engine.sync_database_async())
            )
        except Exception as e:
            sync_status.set_phase("failed", str(e), finished=True)
            logger.error(f"Initial database sync failed: {e}")
            return

        if result == "up_to_date":
            sync_status.set_phase("up_to_date", "Mappings already up-to-date", finished=True)
            logger.info("Initial mapping sync: DB already up-to-date, nothing rebuilt")
        elif result == "synced":
            sync_status.set_phase("done", "Mapping tables rebuilt from Fribb", finished=True)
            logger.info("Initial database sync completed (tables rebuilt)")
        else:
            # sync_database_async already logged the cause, and the previous
            # snapshot is intact.
            sync_status.set_phase("failed", result or "unknown outcome", finished=True)
            logger.warning(f"Initial database sync did not rebuild (outcome={result})")

    spawn(_initial_sync())  # runs off the boot path

    # BackgroundScheduler runs jobs in a worker thread with no running event
    # loop, so the job spins up its own.
    def _scheduled_sync():
        try:
            asyncio.run(db_engine.sync_database_async())
        except Exception as e:
            logger.error(f"Scheduled sync failed: {e}")

    scheduler.add_job(
        _scheduled_sync,
        trigger=IntervalTrigger(hours=24),
        id="db_sync_job",
        replace_existing=True,
    )

    # Nothing upstream reports a TMDB change, so the catalogue is swept
    # oldest-first over a full cycle of nights.
    def _nightly_metadata_refresh():
        try:
            shows, movies = asyncio.run(metadata_maintenance.refresh_daily_slice())
            if shows or movies:
                logger.info(f"Nightly metadata refresh: {shows} show(s), {movies} movie(s)")
        except Exception as e:
            logger.error(f"Nightly metadata refresh failed: {e}")

    scheduler.add_job(
        _nightly_metadata_refresh,
        trigger=CronTrigger(hour=Config.METADATA_REFRESH_HOUR, minute=0),
        id="metadata_nightly_refresh_job",
        replace_existing=True,
    )

    # Backfill jobs the dashboard queues arrive through a table, because the
    # serving replica cannot reach the portless api-sync container.
    def _drain_backfill_queue():
        try:
            asyncio.run(metadata_maintenance.run_pending_backfill())
        except Exception as e:
            logger.error(f"Backfill drain failed: {e}")

    # Polled often so an admin-triggered backfill starts promptly. A run can take
    # minutes, but max_instances=1 skips overlapping ticks so they cannot stack.
    scheduler.add_job(
        _drain_backfill_queue,
        trigger=IntervalTrigger(minutes=1),
        id="metadata_backfill_drain_job",
        replace_existing=True,
    )

    if Config.RUN_METADATA_BACKFILL:
        async def _run_backfill():
            try:
                shows, movies = await metadata_maintenance.backfill_catalogue()
                logger.info(f"Startup metadata backfill seeded {shows} show(s), {movies} movie(s)")
            except Exception as e:
                logger.error(f"Startup metadata backfill failed: {e}")

        spawn(_run_backfill())  # paced internally

    # --- the airing calendar ------------------------------------------------
    # Pinned here rather than run per replica because the refresh rewrites rows
    # every replica reads, and because the notify half opens an SMTP connection.
    # AiringStore.claim makes a stray second replica harmless rather than a
    # duplicate-mail incident, but it should not need to.

    # A fresh deploy would otherwise serve an empty calendar until the first
    # tick, so the window is pulled once off the boot path, like the other
    # warm-ups above.
    async def _warm_airing():
        try:
            written = await airing_notifier.refresh_schedule()
            logger.info(f"Airing schedule warmed: {written} airing(s)")
        except Exception as e:
            logger.error(f"Initial airing refresh failed (will retry on schedule): {e}")

    spawn(_warm_airing())  # must not delay startup

    # Six-hourly: a broadcast slipping is the only thing that changes here, so a
    # tighter interval spends AniList requests to learn nothing.
    def _refresh_airing():
        try:
            asyncio.run(airing_notifier.refresh_schedule())
        except Exception as e:
            logger.error(f"Airing schedule refresh failed: {e}")

    scheduler.add_job(
        _refresh_airing,
        trigger=IntervalTrigger(hours=6),
        id="airing_refresh_job",
        replace_existing=True,
    )

    if not Config.AIRING_NOTIFY_ENABLED:
        logger.info(
            "AIRING_NOTIFY_ENABLED is off, the calendar and follows work but "
            "nobody is mailed when an episode airs"
        )
        return

    if Config.AIRING_NOTIFY_DRY_RUN:
        logger.warning(
            "AIRING_NOTIFY_DRY_RUN is ON: notifications are claimed and logged, "
            "but no SMTP connection is opened and nobody receives anything"
        )

    # Ten-minutely, so a notice follows the broadcast closely. Touches only the
    # database until it has something to send.
    def _notify_airing():
        try:
            result = airing_notifier.send_due_notifications()
            if result["claimed"]:
                logger.info(
                    f"Airing notifications: {result['sent']} sent, "
                    f"{result['failed']} failed, {result['skipped']} skipped"
                )
        except Exception as e:
            logger.error(f"Airing notification run failed: {e}")

    scheduler.add_job(
        _notify_airing,
        trigger=IntervalTrigger(minutes=10),
        id="airing_notify_job",
        replace_existing=True,
    )


async def start_workers(logger: logging.Logger) -> None:
    """Start the download loops this replica is configured to own.

    Both jobs live in Postgres, so work survives an api redeploy and any worker
    can drain the queue. Non-worker replicas still queue and claim rows; they
    just don't run the loop."""
    # Only the dedicated cache-worker runs the ffmpeg loop; api replicas just
    # mint tickets and claim rows.
    if Config.RUN_CACHE_WORKER:
        await cache_manager.start_worker()
    else:
        logger.info(
            "RUN_CACHE_WORKER disabled, this replica mints/claims cache rows but "
            "does not download (the cache-worker service does)"
        )

    # The same split as the cache worker: only the download-worker submits and
    # polls, while other replicas write pending rows and issue pause/resume.
    if Config.RUN_DOWNLOAD_WORKER:
        await download_manager.start_worker()
    else:
        logger.info(
            "RUN_DOWNLOAD_WORKER disabled, this replica queues downloads but does "
            "not run the aria2 poll loop (the download-worker service does)"
        )


async def shutdown(app: FastAPI, logger: logging.Logger) -> None:
    """Drain everything lifespan started, including the HTTP client it opened."""
    logger.info("Shutting down...")
    await cache_manager.stop()
    await download_manager.stop()
    if getattr(app.state, 'scheduler', None) is not None:
        app.state.scheduler.shutdown()
    await close_http_client()
    close_pool()
    logger.info("Shutdown complete")
