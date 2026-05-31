"""Unified collector daemon. The group arg selects which probes run in this process:
  fast = ping + net (10s);  slow = http + mtr + wifi + host;  all = both (one thread).
Production runs `fast` and `slow` as two services so a slow probe never delays ping."""

import sys

from . import adapters, config, core, governor, schema
from .probes import (
    dockerps,
    ext,
    host,
    http,
    inventory,
    logexcerpt,
    mtr,
    net,
    ping,
    pipeline,
    ports,
    redisq,
    synthetic,
    wifi,
)


def _probes(group: str) -> list[tuple[float, str, object]]:
    """(interval, name, collect_fn). The name lets the governor identify which probes to shed."""
    fast = [(config.PING_INTERVAL, "ping", ping.collect), (config.PING_INTERVAL, "net", net.collect)]
    slow = [(config.PROBE_INTERVAL, "http", http.collect), (config.PROBE_INTERVAL, "mtr", mtr.collect),
            (config.PROBE_INTERVAL, "wifi", wifi.collect), (config.HOST_INTERVAL, "host", host.collect),
            (config.PROBE_INTERVAL, "ports", ports.collect)]  # per-port conn counts (stdlib /proc, cheap)
    if config.SYNTHETIC_ENABLED:  # X6: opt-in scripted checks on the slow tier
        slow.append((config.PROBE_INTERVAL, "synthetic", synthetic.collect))
    if config.EXT_HTTP:
        slow.append((config.EXT_INTERVAL, "ext", ext.collect))
    # Auto by default: each of these is registered unless explicitly disabled (=0), and
    # self-detects its dependency at collect time (docker socket / reachable redis / running
    # gst+rtsp), staying a cheap no-op on nodes that don't run the corresponding service.
    if config.REDIS_ENABLED:
        slow.append((config.REDIS_INTERVAL, "redis", redisq.collect))
    if config.DOCKER_ENABLED:
        slow.append((config.DOCKER_INTERVAL, "docker", dockerps.collect))
    if config.PIPELINE_ENABLED:
        slow.append((config.PIPELINE_INTERVAL, "pipeline", pipeline.collect))
    if config.INVENTORY_ENABLED:  # delta-coded device/environment facts (vslow, cheap)
        slow.append((config.INVENTORY_INTERVAL, "inventory", inventory.collect))
    if config.LOGEXCERPT_ENABLED and config.LOGEXCERPT_PATHS:  # event-driven capped log tails
        slow.append((config.LOGEXCERPT_INTERVAL, "logexcerpt", logexcerpt.collect))
    return {"fast": fast, "slow": slow, "all": fast + slow}[group]


def _guarded(name: str, fn, conn):
    """Wrap a probe so the governor can shed it when this process is over budget."""
    def run() -> None:
        shed, reason = governor.should_shed(name)
        if shed:
            governor.note(conn, name, reason)
            return
        fn(conn)
    return run


def main() -> int:
    group = sys.argv[1] if len(sys.argv) > 1 else "all"
    if group not in ("fast", "slow", "all"):
        print(f"usage: collect [fast|slow|all] (got {group!r})", file=sys.stderr)
        return 2
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    schema.init_node(conn)
    probes = [(interval, _guarded(name, fn, conn)) for interval, name, fn in _probes(group)]
    core.log(f"collect start: group={group} node={config.NODE} os={adapters.SYSTEM} db={config.DB_PATH}")
    core.run_scheduler(probes)
    conn.close()
    core.log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
