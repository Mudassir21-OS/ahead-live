"""Test set-up shared by every test file.

By default the tests use a throw-away SQLite file per test (each test sets AHEAD_DB_PATH).

To run the same tests on PostgreSQL, point AHEAD_TEST_DATABASE_URL at a LOCAL database:

    AHEAD_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/ahead_test python -m pytest -q

Every test then starts from an empty schema. The tests refuse any database that isn't on this
computer, so they can never wipe Neon.
"""

import os
from urllib.parse import urlsplit

import pytest

PG_URL = os.environ.get("AHEAD_TEST_DATABASE_URL", "").strip()

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

# Demo accounts and server-configured accounts are never created by tests unless a test sets them.
for name in ("AHEAD_ENABLE_DEMO_ACCOUNTS", "AHEAD_ADMIN_EMAIL", "AHEAD_ADMIN_PASSWORD",
             "AHEAD_DOCTOR_EMAIL", "AHEAD_DOCTOR_PASSWORD"):
    os.environ.pop(name, None)


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
def fresh_database():
    """On PostgreSQL, give each test an empty schema (on SQLite each test already uses a new file)."""
    if PG_URL:
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    reset_caches()
    yield
