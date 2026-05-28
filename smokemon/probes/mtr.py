"""Per-hop latency/loss via mtr --json. Needs root: macOS via passwordless sudo,
Linux can setcap the binary and set SMOKEMON_MTR_SUDO=0."""

import json
import subprocess
import time

from .. import config, core, schema


def _probe(target: str) -> list[dict]:
    cmd = ([config.MTR] if not config.MTR_SUDO else ["sudo", "-n", config.MTR])
    cmd += ["-n", "--json", "-c", str(config.MTR_COUNT), "-i", "0.2", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.MTR_COUNT * 0.2 + 30)
    except Exception as e:  # noqa: BLE001
        core.log(f"mtr error {target}: {e!r}")
        return []
    try:
        hubs = json.loads(proc.stdout)["report"]["hubs"]
    except (ValueError, KeyError) as e:
        core.log(f"mtr parse error {target}: {e!r} (stderr: {proc.stderr[:120]})")
        return []
    return [{"target": target, "hop_no": h.get("count"), "host": h.get("host"), "loss_pct": h.get("Loss%"),
             "sent": h.get("Snt"), "last_ms": h.get("Last"), "avg_ms": h.get("Avg"), "best_ms": h.get("Best"),
             "worst_ms": h.get("Wrst"), "stddev_ms": h.get("StDev")} for h in hubs]


def collect(conn) -> None:
    ts = time.time()
    rows = [{"ts": ts, **hop} for target in config.MTR_TARGETS for hop in _probe(target)]
    if rows:
        schema.insert(conn, "mtr_hops", rows)
        conn.commit()
