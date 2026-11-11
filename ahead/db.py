"""Database access for AHEAD.

Two back ends, one API:

* PostgreSQL when ``DATABASE_URL`` is set (production: Neon). Connections come from a small pool.
* SQLite otherwise (local development and the test suite), at ``AHEAD_DB_PATH``.

Application code writes SQLite-style SQL with ``?`` placeholders. For PostgreSQL this module
translates it: ``?`` becomes a bound parameter, ``INSERT OR IGNORE`` becomes ``ON CONFLICT DO NOTHING``,
and ``cursor.lastrowid`` works through ``RETURNING id``. Rows can be read by column name or by index,
like ``sqlite3.Row``.

Schema changes live in numbered files in ``ahead/migrations``; ``migrate()`` applies each file once
and records it in ``schema_migrations``.
"""

import atexit
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ahead import fieldcrypt

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

try:  # PostgreSQL support is optional for local SQLite use.
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - only without the production requirements
    psycopg = None
    ConnectionPool = None

IntegrityError = (sqlite3.IntegrityError,) + ((psycopg.errors.IntegrityError,) if psycopg else ())


# =============================================================================
# CONFIGURATION
# =============================================================================

def database_url():
    return os.environ.get("DATABASE_URL", "").strip()


def is_postgres():
    return database_url().startswith(("postgres://", "postgresql://"))


def sqlite_path():
    return Path(os.environ.get("AHEAD_DB_PATH", str(BASE / "ahead.sqlite3")))


def target():
    """A key naming the database in use (pools and one-time start-up work are kept per target)."""
    return "pg:" + database_url() if is_postgres() else "sqlite:" + str(sqlite_path())


def describe_target():
    """The database for logs and the health check, without credentials."""
    if not is_postgres():
        return "sqlite"
    host = re.sub(r"^[a-z]+://[^@]*@", "", database_url()).split("/")[0].split("?")[0]
    return f"postgres@{host}"


# =============================================================================
# ROWS AND SQL TRANSLATION (PostgreSQL)
# =============================================================================

class Row(dict):
    """A result row readable as row['name'] and row[0], like sqlite3.Row."""

    __slots__ = ("_values",)

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    """Rows with encrypted health values already decrypted (ahead/fieldcrypt.py)."""
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, fieldcrypt.reveal(values))


def _raw_row_factory(cursor):
    """Rows exactly as stored (the encryption job needs the ciphertext)."""
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _sqlite_row(cursor, values):
    return Row([column[0] for column in cursor.description], fieldcrypt.reveal(values))


def _sqlite_raw_row(cursor, values):
    return Row([column[0] for column in cursor.description], values)


# Tables with an auto-numbered id column: an INSERT into one of them returns the new id.
_INSERT = re.compile(r"^\s*INSERT\s+(OR\s+IGNORE\s+)?INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_ID_TABLES = set()


def _placeholders(sql, has_params):
    """Turn ? into %s outside quoted text, and escape literal % signs for the driver."""
    out, quote = [], None
    for char in sql:
        if quote:
            if char == quote:
                quote = None
            out.append("%%" if char == "%" and has_params else char)
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%" and has_params:
            out.append("%%")
        else:
            out.append(char)
    return "".join(out)


def translate(sql, has_params=True):
    """SQLite-style SQL to PostgreSQL. Returns (sql, inserted_table_or_None)."""
    match = _INSERT.match(sql)
    table = None
    if match:
        table = match.group(2).lower()
        if match.group(1):  # INSERT OR IGNORE
            sql = sql[:match.start(1)] + sql[match.end(1):]
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return _placeholders(sql, has_params), table


class _PgCursor:
    def __init__(self, cursor, lastrowid=None, rows=None):
        self._cursor, self.lastrowid, self._rows = cursor, lastrowid, rows

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        return self._cursor.fetchone()

    def fetchall(self):
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self.fetchall())


class _PgConnection:
    """Wraps a psycopg connection with the small sqlite3-like API the app uses."""

    def __init__(self, conn, decrypt=True):
        self._conn = conn
        self._factory = _row_factory if decrypt else _raw_row_factory

    def execute(self, sql, params=()):
        params = tuple(params) if params is not None else ()
        query, table = translate(sql, bool(params))
        cursor = self._conn.cursor(row_factory=self._factory)
        returning = (table in _ID_TABLES and "RETURNING" not in query.upper()
                     and " SELECT " not in f" {query.upper()} ".replace("\n", " "))
        if returning:
            query = query.rstrip().rstrip(";") + " RETURNING id"
        cursor.execute(query, params or None)
        if returning:
            row = cursor.fetchone()
            return _PgCursor(cursor, lastrowid=row[0] if row else None, rows=[])
        return _PgCursor(cursor)

    def executemany(self, sql, seq):
        seq = [tuple(p) for p in seq]
        if not seq:
            return _PgCursor(self._conn.cursor())
        query, _ = translate(sql, True)
        cursor = self._conn.cursor(row_factory=self._factory)
        cursor.executemany(query, seq)
        return _PgCursor(cursor)

    def executescript(self, script):
        self._conn.execute(script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


# =============================================================================
# CONNECTIONS
# =============================================================================

_pools = {}
_pool_lock = threading.Lock()


# Checking a connection costs a round trip to Neon. A connection handed back in the last 30 s is still
# open (Neon only drops idle ones after minutes), so only older ones are checked before use.
_RECENT_SECONDS = 30
_last_used = {}


def _mark_used(conn):
    now = time.monotonic()
    if len(_last_used) > 200:   # connections are replaced every few minutes; forget the old ones
        for key in [k for k, used in _last_used.items() if now - used > 600]:
            _last_used.pop(key, None)
    _last_used[id(conn)] = now


def _check_if_idle(conn):
    if time.monotonic() - _last_used.get(id(conn), float("-inf")) < _RECENT_SECONDS and not conn.closed:
        return
    ConnectionPool.check_connection(conn)


def _pool():
    url = database_url()
    with _pool_lock:
        pool = _pools.get(url)
        if pool is None:
            if psycopg is None:
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed (pip install -r requirements.txt).")
            pool = ConnectionPool(
                url, min_size=1, max_size=int(os.environ.get("AHEAD_DB_POOL_SIZE", "5")), timeout=20,
                max_idle=120, open=True, check=_check_if_idle, reset=_mark_used, name="ahead",
                kwargs={"cursor_factory": psycopg.ClientCursor, "row_factory": _row_factory,
                        "options": "-c statement_timeout=20000 -c timezone=UTC"},
            )
            _pools[url] = pool
        return pool


@contextmanager
def connection(decrypt=True):
    """One transaction: committed when the block ends, rolled back if it raises.

    Encrypted health values come back decrypted; decrypt=False returns them as stored."""
    if is_postgres():
        with _pool().connection() as conn:
            wrapped = _PgConnection(conn, decrypt)
            try:
                yield wrapped
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = _sqlite_row if decrypt else _sqlite_raw_row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        yield db
        db.commit()
    finally:
        db.close()


def close_pools():
    with _pool_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


atexit.register(close_pools)  # close cleanly when a script or the server exits


def ping():
    """True when the database answers (for the health check)."""
    with connection() as db:
        return db.execute("SELECT 1").fetchone()[0] == 1


# =============================================================================
# MIGRATIONS
# =============================================================================

def _for_dialect(sql):
    """Migration files are written once; PostgreSQL gets identity ids and double-precision numbers."""
    if not is_postgres():
        return sql
    sql = re.sub(r"\bid INTEGER PRIMARY KEY\b", "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", sql)
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def _register_id_tables():
    """Find every table with an auto-numbered id in the migration files (for RETURNING id)."""
    if _ID_TABLES:
        return
    pattern = re.compile(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z_]+)\s*\(\s*id INTEGER PRIMARY KEY", re.IGNORECASE)
    for path in migration_files():
        _ID_TABLES.update(name.lower() for name in pattern.findall(path.read_text(encoding="utf-8")))


_register_id_tables()
_migrated = set()
_migrate_lock = threading.Lock()


def _table_exists(db, name):
    if is_postgres():
        return db.execute("SELECT to_regclass(?) IS NOT NULL", (name,)).fetchone()[0]
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def migrate(legacy_upgrade=None):
    """Apply every migration not yet recorded. Safe to call often: the work happens once per database per process.

    legacy_upgrade(db, run_001) is called for a local SQLite file created before versioned migrations
    existed; it must bring that file to the 001 schema (run_001() creates whatever tables are missing).
    """
    key = target()
    if key in _migrated:
        return
    with _migrate_lock:
        if key in _migrated:
            return
        with connection() as db:
            if is_postgres():
                db.execute("SELECT pg_advisory_xact_lock(4207001)")  # one migrator at a time across instances
            else:
                db.execute("BEGIN IMMEDIATE")
            fresh_tracking = not _table_exists(db, "schema_migrations")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            applied = {row["version"] for row in db.execute("SELECT version FROM schema_migrations")}
            # A local SQLite file from before versioned migrations already holds (part of) the 001 schema.
            pre_versioned = fresh_tracking and not is_postgres() and any(
                _table_exists(db, name) for name in ("users", "records", "screenings"))
            for path in migration_files():
                version = path.stem
                if version in applied:
                    continue
                script = path.read_text(encoding="utf-8")
                if version.startswith("001_") and pre_versioned and legacy_upgrade:
                    legacy_upgrade(db, lambda: _sqlite_script(db, script))
                elif is_postgres():
                    db.executescript(_for_dialect(script))
                else:
                    _sqlite_script(db, script)
                db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                           (version, datetime.now(timezone.utc).isoformat()))
                log.info("applied migration %s", path.name)
        _migrated.add(key)


def _sqlite_script(db, script):
    """executescript() would COMMIT the open transaction; run the statements one by one inside it instead."""
    for statement in split_statements(script):
        db.execute(statement)


def split_statements(script):
    """Split a migration file on semicolons that end a statement (outside quotes and -- comments)."""
    statements, current, quote, i = [], [], None, 0
    while i < len(script):
        char = script[i]
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif script.startswith("--", i):
            end = script.find("\n", i)
            i = len(script) if end == -1 else end
            continue
        elif char == ";":
            text = "".join(current).strip()
            if text:
                statements.append(text)
            current = []
        else:
            current.append(char)
        i += 1
    text = "".join(current).strip()
    if text:
        statements.append(text)
    return statements


def reset_migration_cache():
    """Tests that rebuild a database in place call this so migrate() looks again."""
    _migrated.clear()
