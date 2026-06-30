"""Resolve-telemetry coalescing (telemetry_engine/db.py).

The DB upsert/query need Postgres, but the validation + folding that protects the
table from a hostile/buggy client is pure and worth pinning.
"""

from telemetry_engine.db import MAX_EVENTS_PER_BATCH, MAX_SOURCE_LEN, TelemetryStore


def test_coalesce_sums_ok_and_fail_per_source_env():
    folded = TelemetryStore._coalesce([
        {"source": "Voe", "ok": True},
        {"source": "Voe", "ok": True},
        {"source": "Voe", "ok": False},
        {"source": "Voe", "ok": True, "env": "extension"},
    ])
    assert folded[("Voe", "client")] == [2, 1]
    assert folded[("Voe", "extension")] == [1, 0]


def test_coalesce_defaults_and_validates_env():
    folded = TelemetryStore._coalesce([
        {"source": "X", "ok": True, "env": "WEIRD"},   # unknown env -> client
        {"source": "X", "ok": True, "env": "proxied"},
    ])
    assert folded[("X", "client")] == [1, 0]
    assert folded[("X", "proxied")] == [1, 0]


def test_coalesce_skips_junk_and_blank_sources():
    folded = TelemetryStore._coalesce([
        "not-a-dict",
        {"ok": True},                 # no source
        {"source": "   ", "ok": True},  # blank source
        {"source": "Real", "ok": False},
    ])
    assert folded == {("Real", "client"): [0, 1]}


def test_coalesce_caps_batch_size():
    events = [{"source": f"s{i}", "ok": True} for i in range(MAX_EVENTS_PER_BATCH + 50)]
    folded = TelemetryStore._coalesce(events)
    assert len(folded) == MAX_EVENTS_PER_BATCH


def test_coalesce_truncates_long_source():
    long = "z" * (MAX_SOURCE_LEN + 20)
    folded = TelemetryStore._coalesce([{"source": long, "ok": True}])
    (source, _env), _counts = next(iter(folded.items()))
    assert len(source) == MAX_SOURCE_LEN
