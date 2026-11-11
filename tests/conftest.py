"""Test set-up shared by every test file.

By default the tests use a throw-away SQLite file per test (each test sets AHEAD_DB_PATH).

To run the same tests on PostgreSQL, point AHEAD_TEST_DATABASE_URL at a LOCAL database:

    AHEAD_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/ahead_test python -m pytest -q

Every test then starts from an empty schema. The tests refuse any database that isn't on this
computer, so they can never wipe Neon.

To run everything with health-value encryption on (Phase 3b), also set a test key:

    AHEAD_TEST_FIELD_KEY=$(python -c "from ahead.fieldcrypt import make_key; print(make_key())") python -m pytest -q

Each test then also checks, at the end, that every health value it saved is encrypted in the database
(tests that write plain values on purpose, to imitate data from before 3b, are marked plain_rows).
"""

import os
from urllib.parse import urlsplit

import pytest

PG_URL = os.environ.get("AHEAD_TEST_DATABASE_URL", "").strip()
FIELD_KEY = os.environ.get("AHEAD_TEST_FIELD_KEY", "").strip()
if FIELD_KEY:
    os.environ["AHEAD_FIELD_KEY"] = FIELD_KEY
else:
    os.environ.pop("AHEAD_FIELD_KEY", None)   # a key in the shell must not change what the tests expect

# These check how old local SQLite files are upgraded, so they only make sense on SQLite.
SQLITE_ONLY = {
    "test_older_databases_gain_the_file_name_column",
    "test_valid_rows_are_saved_ready_and_stale_rows_are_repaired",
    "test_old_range_issues_are_rechecked_on_start_up",
    "test_existing_screening_schema_migrates_without_losing_rows",
    "test_tables_migrate_idempotently",
}

if PG_URL:
    host = urlsplit(PG_URL).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"Refusing to run tests against a non-local database ({host}).")
    os.environ["DATABASE_URL"] = PG_URL
elif os.environ.get("DATABASE_URL"):
    # A real DATABASE_URL in the shell must never receive test data: tests use SQLite unless told otherwise.
    os.environ.pop("DATABASE_URL")

# Importing server.py must not start the background encryption and retention job against a test database.
os.environ["AHEAD_STARTUP_MAINTENANCE"] = "0"

# Demo accounts and server-configured accounts are never created by tests unless a test sets them.
for name in ("AHEAD_ENABLE_DEMO_ACCOUNTS", "AHEAD_ADMIN_EMAIL", "AHEAD_ADMIN_PASSWORD",
             "AHEAD_DOCTOR_EMAIL", "AHEAD_DOCTOR_PASSWORD"):
    os.environ.pop(name, None)


def pytest_configure(config):
    config.addinivalue_line("markers", "plain_rows: the test writes unencrypted health values on purpose")
    config.addinivalue_line("markers", "consent_gate: test patients must agree on the consent page like real ones")


def pytest_collection_modifyitems(config, items):
    if not PG_URL:
        return
    skip = pytest.mark.skip(reason="SQLite file-upgrade test")
    for item in items:
        if item.name.split("[")[0] in SQLITE_ONLY:
            item.add_marker(skip)


def reset_caches():
    from ahead import db, platform_data, storage
    db.reset_migration_cache()
    storage._bootstrapped.clear()
    platform_data._started.clear()


@pytest.fixture(autouse=True)
def fresh_database(request):
    """On PostgreSQL, give each test an empty schema (on SQLite each test already uses a new file)."""
    if PG_URL:
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    reset_caches()
    # Test patients have agreed to the consent text (Phase 3b), so older page tests open the page they test;
    # tests of the consent page itself are marked consent_gate and get the real check.
    from unittest import mock
    agreed = None if request.node.get_closest_marker("consent_gate") else \
        mock.patch("ahead.consent.has_consent", return_value=True)
    if agreed:
        agreed.start()
    try:
        yield from _with_encryption_check(request)
    finally:
        if agreed:
            agreed.stop()


def _with_encryption_check(request):
    if not FIELD_KEY:
        yield
        return
    from ahead import db
    # Every database the test touches: SQLite paths are looked up on each connection (modules keep their own
    # reference to db.connection, but connection() always calls db.sqlite_path()).
    seen, original = {"pg:" + PG_URL} if PG_URL else set(), db.sqlite_path

    def watching():
        path = original()
        seen.add(f"sqlite:{path}")
        return path

    # Most tests delete their temporary folder in tearDown, before this fixture ends: look just before that.
    import tempfile
    leaks, cleanup = [], tempfile.TemporaryDirectory.cleanup
    plain_ok = bool(request.node.get_closest_marker("plain_rows"))

    def check_then_cleanup(directory):
        if not plain_ok:
            for target in [t for t in seen if t.startswith(f"sqlite:{directory.name}")]:
                leaks.extend(_plain_health_values(target))
                seen.discard(target)
        return cleanup(directory)

    db.sqlite_path = watching
    tempfile.TemporaryDirectory.cleanup = check_then_cleanup
    try:
        yield
    finally:
        db.sqlite_path = original
        tempfile.TemporaryDirectory.cleanup = cleanup
    if plain_ok:
        return
    for target in seen:
        leaks += _plain_health_values(target)
    assert not leaks, f"health values saved without encryption: {leaks[:5]}"


def _plain_health_values(target):
    """(table.column, start of value) for every stored health value that is not encrypted."""
    import sqlite3
    from ahead.fieldcrypt import EMPTY, PREFIX, SENSITIVE
    found = []
    if target.startswith("sqlite:"):
        path = target[len("sqlite:"):]
        if not os.path.exists(path):
            return []
        conn = sqlite3.connect(path)
        run = lambda sql: conn.execute(sql).fetchall()
        tables = {r[0] for r in run("SELECT name FROM sqlite_master WHERE type='table'")}
    else:
        import psycopg
        conn = psycopg.connect(target[len("pg:"):], autocommit=True)
        run = lambda sql: conn.execute(sql).fetchall()
        tables = {r[0] for r in run("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    try:
        for table, (_, columns) in SENSITIVE.items():
            if table not in tables:
                continue
            for column in columns:
                for (value,) in run(f"SELECT {column} FROM {table}"):
                    if isinstance(value, str) and value not in EMPTY and not value.startswith(PREFIX):
                        found.append((f"{table}.{column}", value[:40]))
    finally:
        conn.close()
    return found
