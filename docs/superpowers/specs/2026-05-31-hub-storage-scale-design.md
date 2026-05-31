# Design: hub-side storage scaling (rollups + optional DuckDB read acceleration)

Date: 2026-05-31
Status: approved

## Goal

Make hub-side aggregate queries stay fast as the hub DB grows, without touching the node
or the product promise (stdlib python, ~30 MB, runs on a Pi; hub runnable with no extra
deps). Two complementary changes, both hub-side only:

1. Rollups / downsampling of the heavy time-series tables (pure stdlib).
2. Optional DuckDB read acceleration that ATTACHes the existing hub SQLite read-only
   (opt-in; graceful fallback to plain SQLite).

## Hard constraints

- Node is untouched: it still ships raw rows exactly as today. No node-side rollups.
- Hub stays runnable with zero new dependencies. DuckDB is opt-in acceleration, never a
  requirement; absence falls back to the current SQLite path.
- Schema changes are additive only (rollup tables + a rollup_state cursor; reuse the
  ensure_body_columns pattern). SQLite remains the master store and the only writer path.
- No fabricated data: every rolled-up value is aggregated from real collected rows.
- Loaders keep returning the exact same dict shapes, so analyze.py, the renderers,
  hubapi.py and the dashboard need no changes.

## Feature 1: hub rollups / downsampling

### Problem
The hub stores raw samples (ping every ~10s per target per node, host every ~30-60s, etc.)
for the whole retention window. Aggregate queries over long spans (heatmap/fleet/ranking
over days) scan millions of rows even with the (node, ts) indexes.

### Approach
New module `smokemon/rollup.py` (hub-side, pure stdlib):

- Rollup tables: for the heavy time-series tables (`ping_runs`, `host_samples`,
  `net_samples`, `tcp_samples`, `wifi_samples`) create `<table>_1m` and `<table>_1h` with
  the same body columns, aggregated per (node, entity, bucket). Per-column aggregation
  mirrors analyze.resample semantics: rates/levels use mean, counters/loss/bandwidth use
  max, identity-ish columns use last. Counter columns (e.g. *_count, *_segs, ibytes) keep
  max so the existing per-second delta loaders still compute correctly across buckets.
- `rollup_state(table, bucket, last_ts)` cursor table so rollups are incremental (only new
  closed buckets are aggregated each pass; the current open bucket is left until it closes).
- `rollup(conn, now)` builds any missing closed buckets since each cursor. Called from the
  hub's existing housekeeping throttle in hub.py (same place ingest_log is pruned), not the
  node prune.py. Skips gracefully when a source table is absent.

### Resolution selection
`query._resolution(since, until)` picks the table suffix by span:
- span <= 6h  -> raw (no suffix)
- span <= 7d  -> "_1m"
- else        -> "_1h"

Each affected loader takes the resolved suffix and reads `<table><suffix>`, returning the
same dict shape as today. When a rollup table is empty/missing (e.g. right after upgrade,
before the first rollup pass) the loader falls back to the raw table so nothing breaks.

## Feature 2: optional DuckDB read acceleration

### Problem
Even with rollups, the heaviest cross-node aggregate endpoints (heatmap, fleet ranking)
are GROUP BY / window scans that a columnar engine runs far faster than row-store SQLite.

### Approach
New module `smokemon/duckio.py` (hub-side):

- Lazy import: `try: import duckdb` sets `_HAS_DUCKDB`. Never a hard import (mirrors
  mlanomaly._HAS_NUMPY) so the hub imports fine without duckdb installed.
- `connect_ro(sqlite_path)` returns a DuckDB connection that ATTACHes the hub SQLite file
  read-only via DuckDB's sqlite extension, or None when duckdb is unavailable.
- `available()` -> bool for callers to branch on.

### Integration
- `hub.py` opens an optional module-level `_duck` connection at startup (after the schema
  exists), guarded by its own usage; SQLite `_ro_conn` stays the master read path and the
  writer path is unchanged.
- The heavy aggregate producers (heatmap, fleet) try DuckDB when available and fall back to
  the existing SQLite implementation otherwise. Both return identical dict shapes, so
  `_cached`, the handlers and the dashboard are untouched.
- Because SQLite stays master, a DuckDB error at query time logs once and falls back to
  SQLite for that request - it can never take the dashboard down.

## Testing

- `tests/test_rollup.py`: rollup() aggregates raw rows into _1m/_1h with correct per-column
  aggregation and is incremental (a second pass adds only new buckets, leaves the open
  bucket); query._resolution picks the right suffix per span; loaders fall back to raw when
  a rollup table is empty.
- `tests/test_duckio.py`: available() reflects import state; when duckdb is importable,
  connect_ro reads the same row counts as SQLite for a seeded DB (importorskip otherwise);
  a forced-unavailable duckio makes the hub producers fall back to SQLite with identical
  output.
- Existing tests must keep passing; ruff clean; changelog entry under unreleased.

## Out of scope

- Node-side rollups (node stays raw + stdlib).
- Replacing SQLite as the hub master store.
- A persistent DuckDB mirror / external TSDB (Prometheus/VictoriaMetrics/Timescale).
- Changing the wire/ship format.
