"""Active throughput (up + down) to a peer via iperf3 -J. Consumes real bandwidth;
run sparsely (timer). Requires `iperf3 -s` on the server."""

import json
import subprocess
import sys
import time

from .. import config, core, schema


def _run(reverse: bool) -> dict | None:
    cmd = [config.IPERF, "-c", config.IPERF_SERVER, "-J", "-t", config.IPERF_DURATION, "--connect-timeout", "5000"]
    if reverse:
        cmd.append("-R")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(config.IPERF_DURATION) + 30)
    except Exception as e:  # noqa: BLE001
        core.log(f"iperf3 error (reverse={reverse}): {e!r}")
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        core.log(f"iperf3 invalid JSON (reverse={reverse}): {proc.stderr[:160]}")
        return None
    if "error" in data:
        core.log(f"iperf3 server error: {data['error']}")
        return None
    return data


def collect(conn) -> None:
    up, down = _run(reverse=False), _run(reverse=True)
    if not up and not down:
        core.log(f"iperf3 no result - is 'iperf3 -s' running on {config.IPERF_SERVER}?")
        return
    row = {
        "ts": time.time(), "server": config.IPERF_SERVER,
        "up_mbps": up["end"]["sum_sent"]["bits_per_second"] / 1e6 if up else None,
        "down_mbps": down["end"]["sum_received"]["bits_per_second"] / 1e6 if down else None,
        "retransmits": up["end"]["sum_sent"].get("retransmits") if up else None,
    }
    schema.insert(conn, "iperf_samples", [row])
    conn.commit()
    core.log(f"iperf3 saved: {config.IPERF_SERVER} up={row['up_mbps'] and round(row['up_mbps'], 1)} "
             f"down={row['down_mbps'] and round(row['down_mbps'], 1)} Mbit/s")


def main() -> int:
    conn = core.connect(config.DB_PATH)
    schema.init_node(conn)
    collect(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
