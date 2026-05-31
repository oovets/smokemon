"""Optional DuckDB read acceleration for the hub (hub-side, read-only).

The hub's heaviest GET endpoints are cross-node GROUP BY / window aggregates. DuckDB is a
columnar OLAP engine that runs those far faster than row-store SQLite, and it can read the
existing hub SQLite file in place via its sqlite extension - so this is a pure read accelerator,
never a second copy and never the writer. SQLite stays the master store.

Guardrail: this is strictly opt-in. duckdb is a lazy import (never a hard top-level dependency),
mirroring mlanomaly._HAS_NUMPY, so the hub imports and runs fine without it installed. Every
caller branches on available() / a None connection and falls back to the plain SQLite path, so
DuckDB can accelerate when present but can never be a requirement or a single point of failure."""

from . import core

try:
    import duckdb as _duckdb
    _HAS_DUCKDB = True
except Exception:  # duckdb is an optional extra; stay importable without it
    _duckdb = None
    _HAS_DUCKDB = False


def available() -> bool:
    """True when the duckdb module is importable (the extra is installed)."""
    return _HAS_DUCKDB


def connect_ro(sqlite_path: str):
    """A DuckDB connection that ATTACHes the hub SQLite file READ_ONLY, or None when duckdb is
    unavailable or the attach fails. Read-only: DuckDB never writes the SQLite file; the hub's
    own sqlite3 writer connection remains the only writer. On any failure we log once and return
    None so the caller falls back to SQLite."""
    if not _HAS_DUCKDB:
        return None
    try:
        con = _duckdb.connect(database=":memory:")
        con.execute("INSTALL sqlite")
        con.execute("LOAD sqlite")
        # ATTACH does not accept a bind parameter, so the path is inlined. It comes from the hub's
        # own config (HUB_DB), not request input; escape single quotes defensively regardless.
        # READ_ONLY so DuckDB never takes a write lock on the file the hub writer owns.
        safe_path = sqlite_path.replace("'", "''")
        con.execute(f"ATTACH '{safe_path}' AS sq (TYPE sqlite, READ_ONLY)")
        con.execute("USE sq")
        return con
    except Exception as e:  # noqa: BLE001 - any duckdb/attach failure must degrade to SQLite
        core.log(f"duckio: DuckDB attach failed, falling back to sqlite: {e!r}")
        return None


def query_rows(con, sql: str, params=()) -> list[tuple]:
    """Run a `?`-parameterised SQL on the DuckDB connection and return a list of tuples, matching
    the shape hubapi._rows returns from sqlite3 so callers can swap engines transparently."""
    return [tuple(r) for r in con.execute(sql, list(params)).fetchall()]
