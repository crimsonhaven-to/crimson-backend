"""Fixture tests for the schema migration runner (core/migrations.py).

Like the rest of the suite these make no network calls and need no database: the
DB-touching half of the runner is exercised against a tiny fake connection that
records what it was asked to execute, so the ordering / idempotency / drift logic
is testable anywhere.

The last two tests are deployment guards. The migration files are ``.sql``, not
Python, so neither the import-graph check in test_dockerfile_copies.py nor a
missing-import error can catch it if they fail to ship: a container would simply
find zero migration files and report itself up to date while being unmigrated.
"""

import re
from pathlib import Path

import pytest

from core import migrations

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- discovery -------------------------------------------------------------
def _write(directory: Path, name: str, body: str = "SELECT 1;\n") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_discover_orders_numerically_not_lexicographically(tmp_path):
    _write(tmp_path, "009_nine.sql")
    _write(tmp_path, "010_ten.sql")
    _write(tmp_path, "002_two.sql")
    found = migrations.discover(tmp_path)
    assert [m.version for m in found] == [2, 9, 10]
    assert [m.name for m in found] == ["two", "nine", "ten"]


def test_discover_ignores_non_migration_files(tmp_path):
    _write(tmp_path, "001_ok.sql")
    _write(tmp_path, "notes.sql")            # no version prefix
    _write(tmp_path, "01_too_short.sql")     # fewer than three digits
    _write(tmp_path, "002_readme.txt")       # not .sql
    assert [m.filename for m in migrations.discover(tmp_path)] == ["001_ok.sql"]


def test_discover_rejects_duplicate_versions(tmp_path):
    _write(tmp_path, "001_first.sql")
    _write(tmp_path, "001_second.sql")
    with pytest.raises(ValueError, match="Duplicate migration version 1"):
        migrations.discover(tmp_path)


def test_discover_on_missing_directory_is_empty(tmp_path):
    assert migrations.discover(tmp_path / "nope") == []


def test_checksum_ignores_line_ending_style():
    """A CRLF checkout (this repo is developed on Windows, built on Linux) must not
    read as drift."""
    assert migrations.checksum("A\r\nB\r\n") == migrations.checksum("A\nB\n")


def test_checksum_detects_real_edits():
    assert migrations.checksum("SELECT 1;") != migrations.checksum("SELECT 2;")


@pytest.mark.parametrize("body,empty", [
    ("-- just a comment\n", True),
    ("/* block */\n\n", True),
    ("   \n\t\n", True),
    ("-- comment\nSELECT 1;\n", False),
])
def test_effectively_empty_detection(body, empty):
    assert migrations._is_effectively_empty(body) is empty


# --- apply_pending against a fake connection -------------------------------
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    """Minimal stand-in for a psycopg connection. Records executed SQL and serves
    whatever `applied` rows the test seeded."""

    def __init__(self, applied_rows=None):
        self.applied_rows = applied_rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split())[:70], params))
        if "FROM schema_migrations" in sql:
            return FakeCursor(self.applied_rows)
        if "INSERT INTO schema_migrations" in sql and params:
            self.applied_rows.append(
                {"version": params[0], "name": params[1], "checksum": params[2],
                 "applied_at": None}
            )
        return FakeCursor([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _migration_ddl(conn):
    """Only the SQL that came from migration FILES, excluding the runner's own
    `CREATE TABLE IF NOT EXISTS schema_migrations` bookkeeping."""
    return [
        sql for sql, _ in conn.executed
        if sql.startswith("CREATE TABLE") and "schema_migrations" not in sql
    ]


@pytest.fixture
def fake_db(monkeypatch):
    """Point the runner at a FakeConn and a no-op advisory lock."""
    conn = FakeConn()
    monkeypatch.setattr(migrations, "get_connection", lambda: conn)
    monkeypatch.setattr(migrations, "lock_schema_init", lambda c: None)
    return conn


def test_apply_pending_applies_in_order_and_records_each(tmp_path, monkeypatch, fake_db):
    _write(tmp_path, "001_a.sql", "CREATE TABLE a (id INT);\n")
    _write(tmp_path, "002_b.sql", "CREATE TABLE b (id INT);\n")
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))

    result = migrations.apply_pending()

    assert result["applied_now"] == ["001_a.sql", "002_b.sql"]
    assert result["version"] == 2
    assert result["drift"] == []

    assert _migration_ddl(fake_db) == ["CREATE TABLE a (id INT);", "CREATE TABLE b (id INT);"]


def test_apply_pending_is_idempotent(tmp_path, monkeypatch, fake_db):
    _write(tmp_path, "001_a.sql", "CREATE TABLE a (id INT);\n")
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))

    first = migrations.apply_pending()
    assert first["applied_now"] == ["001_a.sql"]

    fake_db.executed.clear()
    second = migrations.apply_pending()

    assert second["applied_now"] == []
    assert second["version"] == 1
    assert _migration_ddl(fake_db) == []


def test_apply_pending_skips_comment_only_file_but_records_it(tmp_path, monkeypatch, fake_db):
    """The baseline file is comment-only plus a no-op; Postgres rejects an empty
    query string, so such a file must be recorded without being executed."""
    _write(tmp_path, "000_baseline.sql", "-- nothing to do here\n")
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))

    result = migrations.apply_pending()

    assert result["applied_now"] == ["000_baseline.sql"]
    assert result["version"] == 0
    inserts = [p for sql, p in fake_db.executed if sql.startswith("INSERT INTO schema_migrations")]
    assert len(inserts) == 1


def test_apply_pending_reports_drift_without_reapplying(tmp_path, monkeypatch, fake_db):
    """Editing an already-applied migration must be reported, not silently re-run."""
    _write(tmp_path, "001_a.sql", "CREATE TABLE a (id INT);\n")
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))
    migrations.apply_pending()

    _write(tmp_path, "001_a.sql", "CREATE TABLE a (id BIGINT);\n")  # edited after the fact
    fake_db.executed.clear()
    result = migrations.apply_pending()

    assert result["applied_now"] == []
    assert len(result["drift"]) == 1
    assert result["drift"][0]["version"] == 1
    assert _migration_ddl(fake_db) == []


def test_apply_pending_survives_a_broken_directory(tmp_path, monkeypatch, fake_db):
    """A duplicate version must not raise out of startup: api.py treats migrations
    as non-fatal, and the runner reports the problem rather than propagating it."""
    _write(tmp_path, "001_a.sql")
    _write(tmp_path, "001_b.sql")
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))

    result = migrations.apply_pending()
    assert result["available"] is False
    assert "Duplicate migration version" in result["error"]


def test_cached_status_reflects_last_apply(tmp_path, monkeypatch, fake_db):
    _write(tmp_path, "007_seven.sql", "SELECT 1;\n")
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))
    migrations.apply_pending()
    assert migrations.cached_status()["version"] == 7


# --- deployment guards -----------------------------------------------------
def test_repo_migrations_are_valid_and_start_at_baseline():
    """The real migrations/ directory must parse, and version 0 must exist so
    'unmigrated' stays distinguishable from 'migrated to 0'."""
    found = migrations.discover(REPO_ROOT / "migrations")
    assert found, "migrations/ must contain at least the baseline file"
    assert found[0].version == 0
    versions = [m.version for m in found]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)


def test_migrations_directory_is_copied_into_the_image():
    """Guard for the .sql-shaped blind spot: the migration files are data, not
    Python, so a missing COPY produces a container that finds zero migrations and
    calls itself up to date rather than crashing on an import."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^\s*COPY\s+migrations\s+\./migrations\s*$", dockerfile, re.M), (
        "Dockerfile must COPY the migrations directory into the image"
    )


def test_migrations_are_not_gitignored():
    """The repo ignores *.sql for ad-hoc dumps; migrations/ must be re-included or
    the files never reach the image in the first place."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!migrations/*.sql" in gitignore
