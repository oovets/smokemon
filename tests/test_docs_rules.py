"""The rule table in docs/detector-spec.md must match detect.RULES.

A threshold table in prose drifts from the code the first time someone tunes a number, and a
stale one is worse than none: an operator who reads "trip 92" and sees an incident at 85 will
debug the wrong thing. This test is what lets the doc claim to be authoritative.
"""

import re
from pathlib import Path

from smokemon import detect

SPEC = Path(__file__).resolve().parents[1] / "docs" / "detector-spec.md"


def _documented() -> dict[str, list[str]]:
    """signal -> the table row's cells, parsed out of the markdown table."""
    out = {}
    for line in SPEC.read_text("utf-8").split("\n"):
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 17 and cells[0] not in ("signal", "---------------------"):
            out[cells[0].replace("`", "")] = cells
    return out


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return f"{v:g}" if isinstance(v, float) else str(v)


def test_every_rule_is_documented():
    documented = _documented()
    assert set(documented) == set(detect.RULES) | {"* (fallback)"}


def test_documented_thresholds_match_the_code():
    documented = _documented()
    for signal, rule in detect.RULES.items():
        cells = documented[signal]
        got = [_fmt(rule.kind), _fmt(rule.absolute), rule.direction, _fmt(rule.trip),
               _fmt(rule.clear), _fmt(rule.trip_z), _fmt(rule.clear_z), _fmt(rule.for_s),
               _fmt(rule.clear_for_s), _fmt(rule.cooldown_s), rule.severity, rule.peak_mode,
               _fmt(rule.abs_floor), _fmt(rule.rel_floor), _fmt(rule.min_baseline_n),
               rule.dynamic or "-"]
        assert cells[1:] == got, f"docs/detector-spec.md is stale for {signal}"


def test_worst_case_evidence_arithmetic_is_documented_correctly():
    """The '6 + 24 + 3 = 33' claim is the one a reader sizes their disk budget from."""
    from smokemon import config
    total = (config.INCIDENT_PRE_SAMPLES + config.INCIDENT_DURING_MAX
             + config.INCIDENT_POST_SAMPLES)
    text = SPEC.read_text("utf-8")
    assert (f"{config.INCIDENT_PRE_SAMPLES} + {config.INCIDENT_DURING_MAX} + "
            f"{config.INCIDENT_POST_SAMPLES} = {total}") in text


def test_memory_bound_arithmetic_is_documented_correctly():
    from smokemon import config
    payload = config.SIGNAL_MAX * config.SIGNAL_RING * 3 * 8
    text = SPEC.read_text("utf-8")
    assert f"{payload:,}".replace(",", " ") in text


def test_every_documented_env_var_exists():
    """A doc naming a var config.py does not read sends an operator to set something with no
    effect, which is indistinguishable from the feature being broken."""
    named = set(re.findall(r"`(SMOKEMON_[A-Z0-9_]+)`", SPEC.read_text("utf-8")))
    source = (Path(__file__).resolve().parents[1] / "smokemon" / "config.py").read_text("utf-8")
    missing = {v for v in named if f'"{v}"' not in source}
    assert not missing, f"documented but not read by config.py: {sorted(missing)}"
