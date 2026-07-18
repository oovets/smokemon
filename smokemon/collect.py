"""Unified collector daemon. The group arg selects which probes run in this process:
  fast = ping + net (10s);  slow = wifi + host + inventory + heartbeat;  all = both.
Production runs `fast` and `slow` as two services so a slow probe never delays ping.

Probes sample; they do not decide. Each hands its values to the detector, which keeps them in
a bounded memory window and writes only when a rule confirms an anomaly. A healthy node's only
disk writes are the heartbeat.
"""

import sqlite3
import sys
import time

from . import baseline, config, core, detect, events, expedite, governor, heartbeat, incidents, schema
from .probes import host, inventory, logexcerpt, net, ping, wifi


def _probes(group: str) -> list[tuple[float, str, object]]:
    """(interval, name, collect_fn). The name lets the governor identify which probes to shed."""
    fast = [(config.PING_INTERVAL, "ping", ping.collect),
            (config.PING_INTERVAL, "net", net.collect)]
    if config.SHIP_EXPEDITE and config.HUBS:  # ship elevated events out-of-band, ~10s after they land
        fast.append((config.SHIP_EXPEDITE_INTERVAL, "expedite", expedite.check))
    slow = [(config.PROBE_INTERVAL, "wifi", wifi.collect),
            (config.HOST_INTERVAL, "host", host.collect),
            # Liveness and the slow trends (disk headroom, SD wear, our own DB size). Without
            # it a healthy node writes nothing and the hub cannot tell it from a dead one.
            (config.HEARTBEAT_INTERVAL, "heartbeat", heartbeat.collect),
            # Age-based transitions: close incidents whose signal stopped reporting, and
            # force-close ones that have outlived INCIDENT_MAX_OPEN_S.
            (config.PROBE_INTERVAL, "sweep", _sweep),
            (config.BASELINE_FLUSH_S, "baseline-flush", _flush_baseline)]
    if config.INVENTORY_ENABLED:  # delta-coded device/environment facts (vslow, cheap)
        slow.append((config.INVENTORY_INTERVAL, "inventory", inventory.collect))
    if config.LOGEXCERPT_ENABLED and config.LOGEXCERPT_PATHS:  # event-driven capped log tails
        slow.append((config.LOGEXCERPT_INTERVAL, "logexcerpt", logexcerpt.collect))
    return {"fast": fast, "slow": slow, "all": fast + slow}[group]


def _sweep(conn) -> None:
    incidents.apply(conn, detect.sweep(time.monotonic(), time.time()))


def _flush_baseline(conn) -> None:
    baseline.maybe_flush(conn, time.time())


def _guarded(name: str, fn, conn):
    """Wrap a probe so the governor can shed it when over budget, and so a crash is recorded as an
    event rather than taking the daemon down."""
    def run() -> None:
        shed, why = governor.should_shed(name)
        if shed:
            governor.note(conn, name, why)
            return
        try:
            fn(conn)
        except sqlite3.OperationalError as e:
            # Transient DB contention (another collector holding the write lock): report it, but
            # do not treat it as a probe fault - the next cycle usually succeeds.
            events.trip(conn, f"db:{name}", source="collector", severity="warn",
                        event="db-contention", detail=f"{name}: {e}")
        except Exception as e:  # noqa: BLE001
            core.log(f"probe {name} failed: {e!r}")
            events.trip(conn, f"probe:{name}", source="collector", severity="error",
                        event="probe-crash", detail=f"{name}: {e!r}")
        else:
            events.clear(conn, f"probe:{name}", source="collector", event="probe-recovered",
                         detail=name)
            events.clear(conn, f"db:{name}", source="collector",
                         event="db-contention-recovered", detail=name)
    return run


def main() -> int:
    group = sys.argv[1] if len(sys.argv) > 1 else "all"
    if group not in ("fast", "slow", "all"):
        print(f"usage: collect [fast|slow|all] (got {group!r})", file=sys.stderr)
        return 2
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    schema.init_node(conn)
    baseline.load(conn)
    # Resume incidents that were open when this process last stopped, so a condition that is
    # still broken continues its existing incident instead of opening a second one.
    resumed = incidents.load_open(conn)
    probes = [(interval, _guarded(name, fn, conn)) for interval, name, fn in _probes(group)]
    mode = " DRYRUN" if config.DETECT_DRYRUN else ""
    core.log(f"collect start: group={group} node={config.NODE} db={config.DB_PATH}"
             f" resumed={resumed}{mode}")
    core.run_scheduler(probes)
    baseline.maybe_flush(conn, time.time(), force=True)
    conn.commit()  # a probe that inserted without committing would otherwise roll back on exit
    conn.close()
    core.log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
