"""Multivariate anomaly detection over the analysis frame (hub-side, read-only).

smokemon's tod_anomalies (analyze.py, P1) is univariate: it judges each signal against
its own time-of-day baseline. That misses the dangerous case the project's synchronized
timeline is uniquely good at exposing - several signals co-deviating at once where none is
individually extreme (moderate cpu + moderate temp + a little rtt drift = an emerging
thermal problem). This module scores each time bucket on how jointly anomalous its signals
are, so a correlated cluster of mild deviations rises above the per-signal noise floor.

Two paths, same output:
  - numpy path: a Mahalanobis-style distance over the standardized in-window matrix with a
    ridge-regularized covariance, so correlated co-deviation is rewarded. numpy already
    ships under the existing `png` extra, so this adds no new dependency.
  - stdlib fallback: when numpy is absent, combine the positive robust-z deviations across
    signals per bucket (reusing analyze.robust_z) and flag buckets where >= co_min signals
    co-deviate past z_floor. This keeps the module importable and useful on a bare node.

Guardrail: hub-side and read-only like analyze.py. numpy is imported lazily (never a hard
top-level import) so importing this module never fails on a node with no extras installed.
Every result carries the contributing signals (name, z) so it stays explainable - the same
spirit as analyze.explain_incident, never an opaque score."""

from . import analyze

try:
    import numpy as _np
    _HAS_NUMPY = True
except Exception:  # numpy is an optional extra (png); stay importable without it
    _np = None
    _HAS_NUMPY = False

# Signals scored for anomalies: the higher-is-worse host/network gauges from the frame.
# rssi/mhz are excluded (higher is better there); loss/rtt/cpu/temp/etc. all rise on trouble.
_SIGNALS = ("loss", "rtt", "cpu", "mem", "swap", "temp",
            "psi_cpu", "psi_io", "retry_rate", "retrans", "bw_in", "bw_out")


def _columns(frame: dict) -> list[tuple[str, list]]:
    """(name, series) for every scored signal the frame actually carries data for."""
    out = []
    for name in _SIGNALS:
        series = frame.get("series", {}).get(name)
        if series and any(v is not None and v == v for v in series):
            out.append((name, series))
    return out


def _standardize(series: list) -> tuple[list, float, float]:
    """(robust-z per point or None, center, mad) for one signal against its own window
    baseline. None entries stay None so a bucket missing this signal is skipped for it."""
    center = analyze._median(series)
    mad = analyze._mad(series, center)
    if center is None:
        return [None] * len(series), 0.0, 0.0
    zs = [analyze.robust_z(v, center, mad) if (v is not None and v == v) else None
          for v in series]
    return zs, center, mad


def multivariate_anomalies(frame: dict, z_floor: float = 2.0, co_min: int = 2,
                           score_thresh: float = 3.5, limit: int = 20) -> list[dict]:
    """Rank time buckets by joint anomaly score. Returns [{ts, score, signals:[(name, z)...]}]
    sorted by score desc, capped at `limit`.

    A bucket is reported when at least `co_min` signals are positively deviating past
    `z_floor` AND its combined score clears `score_thresh`. The combined score is the
    Mahalanobis distance (numpy path) or the root-sum-square of the co-deviating robust-z
    values (stdlib path) - both grow with the number and size of simultaneous deviations.
    `signals` lists only the co-deviating signals, largest z first, so the result explains
    itself. Empty when the frame has too few buckets or signals to judge."""
    grid = frame.get("t") or []
    cols = _columns(frame)
    if len(grid) < 3 or len(cols) < co_min:
        return []

    names = [c[0] for c in cols]
    zcols = [_standardize(c[1])[0] for c in cols]  # per-signal robust-z columns
    n = len(grid)

    if _HAS_NUMPY:
        scores = _mahalanobis_scores(zcols, n)
    else:
        scores = None  # stdlib path computes per-bucket below

    out: list[dict] = []
    for b in range(n):
        contrib = []
        for ci, zc in enumerate(zcols):
            z = zc[b]
            if z is not None and z >= z_floor:
                contrib.append((names[ci], round(z, 1)))
        if len(contrib) < co_min:
            continue
        contrib.sort(key=lambda s: -s[1])
        if scores is not None and scores[b] is not None:
            score = scores[b]
        else:
            score = sum(z * z for _, z in contrib) ** 0.5  # root-sum-square of co-deviations
        if score < score_thresh:
            continue
        out.append({"ts": grid[b], "score": round(float(score), 2), "signals": contrib})
    out.sort(key=lambda a: -a["score"])
    return out[:limit]


def _mahalanobis_scores(zcols: list, n: int) -> list:
    """Per-bucket Mahalanobis distance over the standardized signal matrix, or None per
    bucket where too many signals are missing. zcols are already robust-z standardized, so
    the covariance here captures the cross-signal correlation structure; a ridge term keeps
    it invertible when signals are collinear or the window is short."""
    cols = []
    for zc in zcols:
        filled = [z if z is not None else 0.0 for z in zc]  # 0 == "at baseline" after std
        cols.append(filled)
    mat = _np.array(cols, dtype=float).T  # shape (n_buckets, n_signals)
    if mat.shape[0] < 3 or mat.shape[1] < 1:
        return [None] * n
    cov = _np.cov(mat, rowvar=False)
    cov = _np.atleast_2d(cov)
    ridge = 1e-6 * _np.eye(cov.shape[0])
    try:
        inv = _np.linalg.inv(cov + ridge)
    except Exception:
        return [None] * n
    out = []
    for b in range(n):
        row = mat[b]
        d2 = float(row @ inv @ row)
        out.append(d2 ** 0.5 if d2 > 0 else 0.0)
    return out
