"""Startup feature report (core/config_report.py).

It's diagnostics that runs on every boot, so it must never raise and must
honestly reflect which env-gated features are on/off.
"""

from core import config_report


def test_report_has_header_and_a_line_per_feature():
    lines = config_report.build_report()
    assert lines[0].startswith("Crimson feature configuration")
    # One line per static feature, plus any overlay-contributed ones (none in a
    # base build, so this is exact in public CI).
    assert len(lines) == 1 + len(config_report.FEATURES) + len(config_report._overlay_features())


def test_feature_toggles_track_env(monkeypatch):
    # A representative env-gated feature flips on/off with its env var. (The overlay
    # sources are gated the same way but aren't present in a base build.)
    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    off = "\n".join(config_report.build_report())
    assert "[ off] OpenSubtitles subtitles" in off

    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "tok")
    on = "\n".join(config_report.build_report())
    assert "[  on] OpenSubtitles subtitles" in on


def test_missing_proxy_secret_is_a_warning(monkeypatch):
    monkeypatch.delenv("PROXY_SECRET", raising=False)
    report = "\n".join(config_report.build_report())
    assert "[WARN] Proxy signing secret" in report


def test_report_never_leaks_secret_values(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "super-secret-token-value")
    monkeypatch.setenv("PROXY_SECRET", "another-secret")
    report = "\n".join(config_report.build_report())
    assert "super-secret-token-value" not in report
    assert "another-secret" not in report


def test_log_report_never_raises():
    # Even with a broken feature predicate, logging must not blow up startup.
    config_report.log_report()
