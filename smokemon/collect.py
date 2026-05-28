"""Unified collector daemon. The group arg selects which probes run in this process:
  fast = ping + net (10s);  slow = http + mtr + wifi + host;  all = both (one thread).
Production runs `fast` and `slow` as two services so a slow probe never delays ping."""

import sys

from . import adapters, config, core, schema
from .probes import host, http, mtr, net, ping, wifi


def _probes(group: str) -> list[tuple[float, object]]:
    fast = [(config.PING_INTERVAL, ping.collect), (config.PING_INTERVAL, net.collect)]
    slow = [(config.PROBE_INTERVAL, http.collect), (config.PROBE_INTERVAL, mtr.collect),
            (config.PROBE_INTERVAL, wifi.collect), (config.HOST_INTERVAL, host.collect)]
    return {"fast": fast, "slow": slow, "all": fast + slow}[group]


def main() -> int:
    group = sys.argv[1] if len(sys.argv) > 1 else "all"
    if group not in ("fast", "slow", "all"):
        print(f"usage: collect [fast|slow|all] (got {group!r})", file=sys.stderr)
        return 2
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    schema.init_node(conn)
    probes = [(interval, (lambda fn=fn: fn(conn))) for interval, fn in _probes(group)]
    core.log(f"collect start: group={group} node={config.NODE} os={adapters.SYSTEM} db={config.DB_PATH}")
    core.run_scheduler(probes)
    conn.close()
    core.log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
