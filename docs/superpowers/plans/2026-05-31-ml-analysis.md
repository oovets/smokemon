# Three hub-side ML/statistical analysis features — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add robust Theil-Sen death-clock ETAs, incident correlation (storm dedup), and numpy-optional multivariate anomaly detection to smokemon's hub-side analysis, then surface them in `risks()`, the digest, and the dashboard.

**Architecture:** Pure-stdlib additions to `analyze.py` / `query.py` plus one new numpy-optional module `mlanomaly.py`. All read-only, hub-side, no schema change, no edge import. numpy imported lazily with a stdlib fallback so `analyze.py`/`query.py` still run on a bare node.

**Tech Stack:** Python 3.10+ stdlib, numpy (existing `png` extra, optional), pytest, ruff.

---

### Task 1: Theil-Sen robust ETA (Feature 2)

**Files:**
- Modify: `smokemon/query.py` (add `theil_sen_eta_seconds`, switch `_soonest_eta`)
- Test: `tests/test_query.py`

- [ ] Add `theil_sen_eta_seconds(t, vals, target, max_pts=200)`: median of pairwise slopes (bounded by subsampling to `max_pts`), same return contract as `linear_eta_seconds` (None when <3 pts / non-finite span / slope not toward target; 0.0 when already past target).
- [ ] Switch `_soonest_eta` to call the robust estimator (keep `linear_eta_seconds` for back-compat).
- [ ] Tests: a clean linear ramp gives ~same ETA as linear; a ramp with one wild outlier gives a robust (non-blown-up) ETA where linear would be skewed; flat series -> None; already-past -> 0.0.

### Task 2: Incident correlation (Feature 3)

**Files:**
- Modify: `smokemon/analyze.py` (add `correlate_incidents`)
- Test: `tests/test_analyze.py`

- [ ] Add `correlate_incidents(incidents, window_s=120.0)`: sort by start, group incidents whose span is within `window_s` of the running group span (interval-merge like `merge_spans`), each group = `{start, end, members, root, severity}` where root = highest-severity member tie-broken by earliest start. Raw members retained.
- [ ] Tests: two overlapping incidents -> one group with both members and the higher-severity root; two far-apart incidents -> two groups; empty -> empty.

### Task 3: Multivariate anomaly module (Feature 1)

**Files:**
- Create: `smokemon/mlanomaly.py`
- Test: `tests/test_mlanomaly.py`

- [ ] `multivariate_anomalies(frame, z_floor=2.0, score_thresh=...)`: build the standardized in-window matrix from `frame["series"]` (robust center/MAD per signal via `analyze`), then:
  - numpy path (`_HAS_NUMPY`): per-bucket Mahalanobis distance on the standardized matrix with a regularized covariance.
  - stdlib fallback: per-bucket combine of positive robust-z across signals, flag when >=2 signals co-deviate past `z_floor`.
  - Return `[{ts, score, signals:[(name, z)...]}]` ranked desc; always include contributing signals.
- [ ] Tests: stdlib path flags a synthetic bucket where 3 signals co-deviate mildly while none is an extreme univariate spike; quiet frame -> no anomalies. numpy path tested under a `pytest.importorskip("numpy")` guard.

### Task 4: Surface integration

**Files:**
- Modify: `smokemon/hubapi.py` (`risks()` add `anomalies` tier + grouped incidents)
- Modify: `smokemon/report.py` (`digest` / `incidents_report` mention anomalies + groups)
- Modify: `smokemon/hubapi.py` `_DASHBOARD_HTML` (render anomalies tier in risks tab)
- Test: `tests/test_hubapi.py`, `tests/test_report.py`, `tests/test_hub.py`

- [ ] `risks()`: per node, build a frame and call `mlanomaly.multivariate_anomalies`; add an `anomalies` list to the return; group `incidents` via `analyze.correlate_incidents` (expose groups without dropping raw incidents).
- [ ] `digest`: when anomalies exist, add a line naming the top multivariate anomaly + its contributing signals.
- [ ] Dashboard: render the `anomalies` tier in the risks modal/tab; add a `test_hub.py` assertion that the markup exists.

### Task 5: Verify + changelog

- [ ] `ruff check .` clean.
- [ ] `python -m pytest` green.
- [ ] Add a CHANGELOG.md entry under the unreleased block (house style: lowercase, inside the fenced box).

## Self-review

Spec coverage: F1 -> Task 3, F2 -> Task 1, F3 -> Task 2, surfaces -> Task 4, testing/changelog -> Task 5. No placeholders. Names consistent: `theil_sen_eta_seconds`, `correlate_incidents`, `multivariate_anomalies`.
