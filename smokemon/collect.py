"""The node daemon: sample, detect, ship, prune. One process, one SQLite connection.

This used to be five units -- two collectors split fast/slow, plus a shipper timer and a prune
timer. The split existed because mtr took 32 s, http fetched URLs serially and iperf ran for
seconds. All three are gone, and what remains cannot delay ping meaningfully. Merging them
removes what the split actually cost: two daemons contending for one SQLite write lock, which
is the reason db-contention events and the concurrent-ALTER guard exist at all.

Shipping and pruning fold in for the same reason. A systemd timer firing `python -m
smokemon.ship` every 15 s spent ~200 ms of Pi CPU on interpreter startup, 5760 times a day,
usually to find nothing to send -- the largest single cost in an agent whose whole claim is a
small footprint.

Probes sample; they do not decide. Each hands its values to the detector, which keeps them in a
bounded memory window and writes only when a rule confirms an anomaly. A healthy node's only
periodic disk write is the heartbeat.
"""

import sqlite3
import sys
import time

from . import baseline, config, core, detect, events, governor, heartbeat, incidents, prune, schema, ship
from .probes import host, inventory, logexcerpt, net, ping, wifi

# How often to consider shipping -- not how often we ship. ship.tick() sends when an elevated
# event is waiting or SHIP_INTERVAL has elapsed, so this only bounds how quickly an incident
# can leave the node.
SHIP_TICK = 10.0


def _probes() -> list[tuple[float, str, object]]:
    """(interval, name, collect_fn). The name lets the governor identify which probes to shed."""
    tasks = [
        (config.PING_INTERVAL, "ping", ping.collect),
        (config.PING_INTERVAL, "net", net.collect),
        (config.PROBE_INTERVAL, "wifi", wifi.collect),
        (config.HOST_INTERVAL, "host", host.collect),
        # Liveness and the slow trends (disk headroom, SD wear, our own DB size). Without it a
        # healthy node writes nothing and the hub cannot tell it from a dead one.
        (config.HEARTBEAT_INTERVAL, "heartbeat", heartbeat.collect),
        # Age-based transitions: close incidents whose signal stopped reporting, and force-close
        # ones that have outlived INCIDENT_MAX_OPEN_S.
        (config.PROBE_INTERVAL, "sweep", _sweep),
        (config.BASELINE_FLUSH_S, "baseline-flush", _flush_baseline),
    ]
    if config.HUBS:
        tasks.append((SHIP_TICK, "ship", ship.tick))
    if config.INVENTORY_ENABLED:  # delta-coded device/environment facts (vslow, cheap)
        tasks.append((config.INVENTORY_INTERVAL, "inventory", inventory.collect))
    if config.LOGEXCERPT_ENABLED and config.LOGEXCERPT_PATHS:  # event-driven capped log tails
        tasks.append((config.LOGEXCERPT_INTERVAL, "logexcerpt", logexcerpt.collect))
    tasks.append((config.PRUNE_INTERVAL, "prune", _prune))
    return tasks


def _sweep(conn) -> None:
    incidents.apply(conn, detect.sweep(time.monotonic(), time.time()))


def _flush_baseline(conn) -> None:
    baseline.maybe_flush(conn, time.time())


def _prune(conn) -> None:
    """Daily retention sweep, in-process. Was a systemd timer; there is nothing here that
    needs its own interpreter."""
    if config.RETENTION_DAYS <= 0:
        return
    deleted = prune.prune(conn)
    total = sum(deleted.values())
    if not total:
        return
    # Truncate the WAL so the on-disk footprint actually drops after a large delete; without
    # it the freed pages stay in the log and the card sees no benefit.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as e:
        core.log(f"prune: wal_checkpoint failed: {e!r}")
    detail = ", ".join(f"{t}={n}" for t, n in sorted(deleted.items(), key=lambda kv: -kv[1]))
    core.log(f"prune: deleted {total} rows ({detail}) older than {config.RETENTION_DAYS}d")


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
            # do not treat it as a probe fault - the next cycle usually succeeds. uid: best-effort
            # link to whatever incident was open when it happened, not causal proof.
            events.trip(conn, f"db:{name}", source="collector", severity="warn",
                        event="db-contention", detail=f"{name}: {e}",
                        uid=incidents.active_uid(conn))
        except Exception as e:  # noqa: BLE001
            core.log(f"probe {name} failed: {e!r}")
            events.trip(conn, f"probe:{name}", source="collector", severity="error",
                        event="probe-crash", detail=f"{name}: {e!r}",
                        uid=incidents.active_uid(conn))
        else:
            events.clear(conn, f"probe:{name}", source="collector", event="probe-recovered",
                         detail=name)
            events.clear(conn, f"db:{name}", source="collector",
                         event="db-contention-recovered", detail=name)
    return run


def main(argv: list[str] | None = None) -> int:
    # The fast/slow group argument is gone but still accepted, so an old unit file that
    # survives an upgrade starts the daemon rather than exiting 2 and looking like a crash.
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("fast", "slow"):
        core.log(f"note: '{argv[0]}' is obsolete -- the collector is one process now")
    core.install_signals()
    conn = core.connect(config.DB_PATH)
    schema.init_node(conn)
    ship.init_state(conn)
    baseline.load(conn)
    # Resume incidents that were open when this process last stopped, so a condition that is
    # still broken continues its existing incident instead of opening a second one.
    resumed = incidents.load_open(conn)
    probes = [(interval, _guarded(name, fn, conn)) for interval, name, fn in _probes()]
    mode = " DRYRUN" if config.DETECT_DRYRUN else ""
    hubs = len(config.HUBS)
    core.log(f"smokemon start: node={config.NODE} db={config.DB_PATH} hubs={hubs}"
             f" resumed={resumed}{mode}")
    core.run_scheduler(probes)
    baseline.maybe_flush(conn, time.time(), force=True)
    conn.commit()  # a probe that inserted without committing would otherwise roll back on exit
    conn.close()
    core.log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
