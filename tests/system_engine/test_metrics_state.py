from core import db_pool
from system_engine import metrics_state


def test_the_export_survives_every_subsystem_being_down(monkeypatch):
    """With the database unreachable the export loses gauges, not the whole
    scrape: a /metrics that dies during an incident is the wrong failure."""
    def _explode(*a, **kw):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(db_pool, "pool_stats", _explode)
    monkeypatch.setattr(metrics_state, "telemetry_rows", _explode)

    names = {f.name for f in metrics_state.StateCollector().collect()}
    assert any("build_info" in n for n in names)
    assert not any("db_pool" in n for n in names)


def test_the_telemetry_gauge_is_capped(monkeypatch):
    """Source names come from a client beacon, so a hostile client could invent
    endless names; each would become a permanent series."""
    rows = [{"source": f"injected-{i}", "ok": 1, "fail": 0, "success_rate": 1.0} for i in range(500)]
    monkeypatch.setattr(metrics_state.telemetry_store, "top_stats", lambda days: rows)
    monkeypatch.setattr(metrics_state, "_telemetry_cache", (0.0, []))

    families = {f.name: f for f in metrics_state.StateCollector().collect()}
    assert len(families["crimson_source_success_ratio"].samples) <= metrics_state.TELEMETRY_TOP_N


def test_worker_stats_need_no_started_worker():
    """The collector calls these on every scrape."""
    from cache_engine.downloader import manager as cache_manager

    stats = cache_manager.worker_stats()
    assert stats["queued"] == 0 and stats["inflight"] == 0
    assert stats["running"] is False
