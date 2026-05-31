# Design: three hub-side ML/statistical analysis features

Date: 2026-05-31
Status: approved (pending user spec review)

## Goal

Add three higher-value analysis capabilities on top of smokemon's existing
hub-side analysis engine, without touching the edge footprint. All three reuse
the existing `analyze.build_frame()` / `query.py` loaders, are read-only, and
surface through the existing `risks()` (dashboard) and `digest` / `incidents`
(CLI) paths.

## Hard constraints (non-negotiable)

- Node-side code stays stdlib-only. None of this is imported by
  `collect` / `ship` / `probes` / `adapters`.
- `analyze.py` and `query.py` must keep running on a node with no extras
  installed (the module docstrings promise this). Therefore numpy is imported
  lazily with a pure-stdlib fallback; it is never a hard top-level import.
- No schema changes are required. Everything is computed at render/request time
  from already-collected real metrics. (If we later persist scores it must be an
  additive `ensure_body_columns` migration; out of scope here.)
- No fabricated/sample data. Every number is derived from real rows.

## Feature 1: multivariate anomaly detection

### Problem
`analyze.tod_anomalies` is univariate: it judges each signal against its own
time-of-day baseline. It misses dangerous *combinations* where several signals
each look individually normal but co-deviate (e.g. moderate cpu + moderate temp
+ slight rtt drift = an emerging thermal problem).

### Approach
New module `smokemon/mlanomaly.py` (hub-side, numpy-optional):

- Input: the columns from `analyze.build_frame()` (already resampled onto one
  common grid).
- Primary path (numpy, available because numpy ships under the existing `png`
  extra): build a robust covariance over the in-window matrix of standardized
  signals and compute a per-bucket Mahalanobis-style distance. This captures
  correlated co-deviation, not just per-axis spikes.
- Fallback path (pure stdlib, no numpy): per-bucket aggregate of the
  positive robust-z deviations across signals (reusing `analyze.robust_z` and
  `analyze.tod_baseline`), flagging a bucket when >= 2 signals co-deviate past a
  mild threshold even if none crosses the univariate bar. The stdlib path keeps
  `analyze.py`'s "runs unchanged on a node" promise intact.
- Output: a list of `{ts, score, signals: [(name, z), ...]}` ranked by score.
  The contributing signals are always returned so the result stays explainable
  (same spirit as `explain_incident`), never a black-box score.

### Why not Isolation Forest / sklearn
Rejected during brainstorming: a new sklearn extra + offline training pipeline
is far more dependency/operational weight than the value justifies here, and
breaks the "no extra training step" simplicity. numpy is already present
hub-side under `png`, so the Mahalanobis path is "real" multivariate ML with
zero new dependencies.

## Feature 2: robust death-clock ETAs (Theil-Sen)

### Problem
`query.linear_eta_seconds` is ordinary least-squares. It is sensitive to
outliers and cache swings, which is exactly why memory was deliberately excluded
from the death clocks (the comment in `hubapi.risks()` says mem% near 100 with
cache produces too many false ETAs).

### Approach
Add `query.theil_sen_eta_seconds(t, vals, target)`:
- Theil-Sen slope = median of pairwise slopes between sample points. Robust to
  outliers, pure stdlib, same signature and return contract as
  `linear_eta_seconds` (None when not projectable, 0.0 when already past target).
- `disk_full_eta` / `wear_eta` switch to the robust estimator via the shared
  `_soonest_eta` helper. `linear_eta_seconds` is kept (back-compat / tests).
- Pairwise enumeration is bounded: if there are more than N points we sample a
  capped subset so the median-of-pairs stays O(N) on the hub, not O(N^2).

Confidence intervals / re-introducing the memory clock are explicitly out of
scope for this change (possible follow-up).

## Feature 3: incident / alert correlation (storm dedup)

### Problem
Incidents and service alerts are evaluated per `node/kind/label`. A single root
cause (e.g. thermal throttling) trips several correlated alerts at once, so the
recipient gets a storm of separate rows.

### Approach
Add `analyze.correlate_incidents(incidents, window_s)` (pure stdlib):
- Group incidents whose time spans fall within `window_s` of each other into a
  single correlated group (reuses the `merge_spans` interval logic).
- Each group exposes: the merged span, member incidents, and a likely root
  (highest-severity member, tie-broken by earliest start).
- The raw members are always retained under the group so a genuine second fault
  is never hidden.

## Surfaces

- `hubapi.risks()`: add an `anomalies` tier (Feature 1) and group incidents via
  Feature 2; clocks already flow through `disk_full_eta`/`wear_eta` so they
  pick up Theil-Sen automatically.
- `report.digest` / `report.incidents_report`: mention multivariate anomalies
  and grouped incidents in the narrative.
- Dashboard (`hubapi.py` `_DASHBOARD_HTML`): render the new `anomalies` tier and
  the grouped-incident structure in the risks tab.

## Testing

New tests under `tests/test_analyze.py` (correlation, stdlib anomaly path) and
`tests/test_query.py` (Theil-Sen ETA). The numpy anomaly path is tested when
numpy is importable and skipped otherwise (mirrors how png tests guard the
optional extra). Existing tests must keep passing; `ruff check .` clean;
changelog entry under the unreleased block.

## Out of scope

- LLM digest narration (Feature 4 from the analysis).
- Per-node learned thresholds (Feature 5).
- On-device Jetson autoencoder (X3, already deferred).
- Persisting anomaly scores to the DB.
