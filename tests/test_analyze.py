"""Hub-side shaping of stored incidents: robust statistics, target classification and
incident correlation - smokemon.analyze. All pure, all stdlib."""

import math

from smokemon import analyze

# ---------- pure stats ----------

def test_robust_z_and_constant_baseline():
    assert analyze.robust_z(10.0, 10.0, 2.0) == 0.0
    assert analyze.robust_z(20.0, 10.0, 0.0) == 50.0     # constant baseline, value above
    assert analyze.robust_z(5.0, 10.0, 0.0) == -50.0
    z = analyze.robust_z(14.826, 0.0, 1.0)
    assert math.isclose(z, 10.0, rel_tol=1e-3)


# ---------- target classification ----------

def test_classify_target():
    assert analyze.classify_target("192.168.0.1") == "gw"
    assert analyze.classify_target("10.0.0.1") == "gw"
    assert analyze.classify_target("100.100.100.100") == "tailscale"
    assert analyze.classify_target("1.1.1.1") == "internet"


def test_severity_rank_orders_words_and_defaults_unknown_to_warn():
    ranks = [analyze.severity_rank(s) for s in ("info", "warn", "error", "crit")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    # An unrecognised rule severity must not drop below the paging bar silently.
    assert analyze.severity_rank("brand-new") == analyze.severity_rank("warn")
    assert analyze.severity_rank(None) == analyze.severity_rank("warn")


# ---------- incident correlation / storm dedup ----------

def test_correlate_incidents_groups_overlapping():
    """Three incidents firing in the same window collapse into one group whose root is the
    highest-severity member; all three are kept as members."""
    incs = [
        {"start": 100.0, "end": 160.0, "severity": 1, "klass": "latency-spike"},
        {"start": 120.0, "end": 200.0, "severity": 3, "klass": "isp-outage"},
        {"start": 210.0, "end": 240.0, "severity": 2, "klass": "packet-loss"},
    ]
    groups = analyze.correlate_incidents(incs, window_s=120.0)
    assert len(groups) == 1
    g = groups[0]
    assert g["start"] == 100.0 and g["end"] == 240.0
    assert g["severity"] == 3
    assert g["root"]["klass"] == "isp-outage"
    assert len(g["members"]) == 3


def test_correlate_incidents_splits_distant():
    """Incidents separated by more than window_s stay in their own groups (a genuine second
    fault is not folded into the first)."""
    incs = [
        {"start": 100.0, "end": 130.0, "severity": 2, "klass": "packet-loss"},
        {"start": 1000.0, "end": 1030.0, "severity": 2, "klass": "latency-spike"},
    ]
    groups = analyze.correlate_incidents(incs, window_s=120.0)
    assert len(groups) == 2
    assert [len(g["members"]) for g in groups] == [1, 1]


def test_correlate_incidents_empty():
    assert analyze.correlate_incidents([]) == []
