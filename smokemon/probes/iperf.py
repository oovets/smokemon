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


def _under_load_rtt_ms(data: dict | None) -> float | None:
    """Mean TCP RTT (ms) across streams during the loaded transfer. iperf3 reports
    per-stream sender.mean_rtt in microseconds (TCP only; absent on UDP or platforms
    that omit tcp_info). This is the link's round-trip while the pipe is saturated, so
    paired with the idle ping baseline it yields a dslreports-style bufferbloat grade.
    Returns None when no stream reports an RTT."""
    if not data:
        return None
    rtts = []
    for s in data.get("end", {}).get("streams", []):
        rtt = (s.get("sender") or {}).get("mean_rtt")
        if rtt:  # 0 or None means the platform did not report tcp_info
            rtts.append(rtt / 1000.0)
    return sum(rtts) / len(rtts) if rtts else None


def collect(conn) -> None:
    if not config.IPERF_SERVER:
        core.log("iperf: SMOKEMON_IPERF_SERVER not set, skipping")
        return
    up, down = _run(reverse=False), _run(reverse=True)
    if not up and not down:
        core.log(f"iperf3 no result - is 'iperf3 -s' running on {config.IPERF_SERVER}?")
        return
    try:
        # A result without an "error" key can still lack end/sum_* if the test was cut
        # short (server timeout mid-run); guard so a partial JSON never crashes collect.
        up_sum = up["end"]["sum_sent"] if up else None
        row = {
            "ts": time.time(), "server": config.IPERF_SERVER,
            "up_mbps": up_sum["bits_per_second"] / 1e6 if up_sum else None,
            "down_mbps": down["end"]["sum_received"]["bits_per_second"] / 1e6 if down else None,
            "retransmits": up_sum.get("retransmits") if up_sum else None,
            # Loaded RTT from the forward (uplink-saturating) test - the classic
            # home-bufferbloat direction. Falls back to the reverse test if the
            # forward run produced no stream RTTs.
            "rtt_under_load_ms": _under_load_rtt_ms(up) or _under_load_rtt_ms(down),
        }
    except (KeyError, TypeError) as e:
        core.log(f"iperf3 incomplete result, skipping: {e!r}")
        return
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
