# Hub storage scaling (rollups + DuckDB) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Keep hub aggregate queries fast as the DB grows via stdlib rollups, plus an optional DuckDB read accelerator that ATTACHes the SQLite hub read-only. Node untouched; hub runs with zero new deps (DuckDB opt-in, graceful fallback).

**Architecture:** New hub-side modules `smokemon/rollup.py` (stdlib downsampling into additive `*_1m`/`*_1h` tables driven by a `rollup_state` cursor) and `smokemon/duckio.py` (lazy `duckdb`, ATTACH SQLite read-only). `query._resolution()` chooses raw/1m/1h by span; loaders fall back to raw when a rollup table is empty. SQLite stays master + sole writer.

**Tech Stack:** Python 3.10+ stdlib, sqlite3, duckdb (optional), pytest, ruff.

---

### Task 1: rollup schema + builder (stdlib)

**Files:**
- Create: `smokemon/rollup.py`
- Modify: `smokemon/schema.py` (rollup table + rollup_state DDL helpers, hub-side)
- Test: `tests/test_rollup.py`

- [ ] Define `ROLLUP_TABLES = ("ping_runs","host_samples","net_samples","tcp_samples","wifi_samples")`, buckets `{"_1m":60,"_1h":3600}`, and a per-column aggregation map derived from `schema._BODY` (mean for levels/rates, max for counters/loss/bandwidth/`*_count`/`*_segs`/`bytes`, last for identity-ish text cols; group key = node + the table's `_IX` entity + bucket).
- [ ] `ensure_rollup_tables(conn)` (hub-side): create `<table><suffix>` with the same body cols + node + a `bucket_ts REAL` and `rollup_state(table TEXT, bucket TEXT, last_ts REAL, PRIMARY KEY(table,bucket))`. Additive/IF NOT EXISTS. Wire into `schema.init_hub`.
- [ ] `rollup(conn, now=None)`: for each table×bucket, read `last_ts`, aggregate raw rows in closed buckets `(ts < now - (now % size))` newer than `last_ts` grouped by (node, entity, bucket_ts) with the per-column agg, INSERT into the rollup table, advance the cursor. Returns `{("table","bucket"): rows_written}`. Skip absent source tables.
- [ ] Tests: seed raw host_samples across 3 minutes; rollup() writes 1m buckets with mean cpu == raw mean per minute; a second pass with no new closed bucket writes 0; the still-open current bucket is not rolled up.

### Task 2: resolution-aware loaders

**Files:**
- Modify: `smokemon/query.py` (add `_resolution`, thread suffix into the heavy loaders)
- Test: `tests/test_rollup.py`

- [ ] `_resolution(since, until)`: span<=6h -> "", <=7d -> "_1m", else "_1h".
- [ ] `_table_for(conn, base, suffix)`: return `base+suffix` when that table exists AND has any row in range, else `base` (raw fallback). Keep it read-only/`_q`-safe.
- [ ] Thread an optional `res=None` into `load_host`/`load_ping_agg` (compute via `_resolution` when None) so they read the resolved table but return the identical dict shape. Default behavior (res="" / raw) unchanged for callers that don't pass a window hint.
- [ ] Tests: `_resolution` thresholds; `load_host` returns identical shape from a populated `_1m` table; falls back to raw when `_1m` is empty.

### Task 3: DuckDB read module (optional)

**Files:**
- Create: `smokemon/duckio.py`
- Test: `tests/test_duckio.py`

- [ ] Lazy `try: import duckdb` -> `_HAS_DUCKDB`. `available()` -> bool. `connect_ro(sqlite_path)`: when available, `duckdb.connect()`, `INSTALL/LOAD sqlite`, `ATTACH '<path>' AS sq (TYPE sqlite, READ_ONLY)`, `USE sq`; return the connection or None on any failure (log once).
- [ ] `query_rows(duck, sql, params)`: run a `?`-param SQL and return a list of tuples (parity with `hubapi._rows`).
- [ ] Tests: `available()` matches import state; under `importorskip("duckdb")`, connect_ro on a seeded hub DB returns the same `COUNT(*)` for ping_runs as sqlite3; connect_ro returns None when `_HAS_DUCKDB` is monkeypatched False.

### Task 4: wire DuckDB + rollups into the hub

**Files:**
- Modify: `smokemon/hubapi.py` (`heatmap` tries DuckDB then falls back; reuse same SQL)
- Modify: `smokemon/hub.py` (open optional `_duck`; call `rollup()` in housekeeping)
- Test: `tests/test_hubapi.py`, `tests/test_hub.py`

- [ ] `heatmap(conn, ..., duck=None)`: build the SQL once; when `duck` is provided run it via `duckio.query_rows`, else `_rows(conn, ...)`. Identical post-processing -> identical dict.
- [ ] `hub.py`: at startup after schema init, set module `_duck = duckio.connect_ro(config.HUB_DB)` (None when unavailable). In the heatmap GET, pass `_duck`. In the existing housekeeping throttle (where ingest_log is pruned), also call `rollup.rollup(_conn)` under `_lock`.
- [ ] Tests: `heatmap` with a forced `duck=None` equals today's output; a `test_hub` assertion that the hub imports and `duckio.available()` is callable; rollup invoked path doesn't error on an empty DB.

### Task 5: verify + changelog

- [ ] `ruff check .` clean on changed files.
- [ ] `python -m pytest` green.
- [ ] CHANGELOG unreleased entry (lowercase house style) describing hub rollups + optional DuckDB read acceleration, node untouched, graceful fallback.

## Self-review

Spec coverage: F1 -> Tasks 1,2,4; F2 -> Tasks 3,4; testing/changelog -> Task 5. Names consistent: `ensure_rollup_tables`, `rollup`, `rollup_state`, `_resolution`, `_table_for`, `duckio.connect_ro`, `duckio.available`, `query_rows`. No placeholders.
