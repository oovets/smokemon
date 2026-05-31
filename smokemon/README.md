# smokemon/

> the python package. one codebase runs three roles — a **node** collects + ships, a **hub**
> ingests + serves, and **read surfaces** (cli / tui / png / text) draw whatever's in a DB. the
> collection path is stdlib-only and stays at ~30 mb rss on a pi/jetson; the heavier deps
> (matplotlib/numpy for png, plotext for the tui) live only on the read + hub side.

every module is small and single-purpose. the split below is the whole architecture: a probe
samples, the scheduler runs it, the shipper pushes deltas, the hub ingests and serves, and the
analysis/render layers only ever *read* a populated DB. nothing here daemonizes itself — launchd
(macOS) or systemd (Linux) runs the entrypoints.

```
== data flow ==

node                                            hub
  collect (fast/slow) -> probes/ -> smokemon.db    POST /ingest (gzip delta, X-Smokemon-Key)
                          ^  adapters/ (os reads)        |
  events -> ext_events ---+                              v
  expedite ---------------+--> ship --------------> hub  -> smokemon-hub.db
  governor (sheds)                                   |  rollup (1m/1h)  alerts (notify)
  prune (retention)                                  v
                                            hubapi -> GET / · /metrics · /api/*
                                            analyze + mlanomaly (read-only)

read surfaces (any DB; node or hub):
  cli -> query -> render/{tui,png}            graphed panels of one host
  cli -> report -> analyze                    status / incidents / digest (text, stdlib)
  cli -> footprint                            rows/day + ship bytes/day estimate
```

## runtime core (imported everywhere)

```
__init__.py    package marker + __version__ (the one place the version string lives).
config.py      all SMOKEMON_* env vars, node identity, paths, tool-path lookup, render
               constants. tri-state _enabled() (unset=auto, 0/off=disabled) backs the
               auto/opt-in probe gating. the single place config is read from the environment.
core.py        the daemon runtime shared by every collector: log(), connect() (WAL sqlite),
               install_signals(), run_scheduler() and a stable per-node _jitter() so a fleet
               fires on the same wall-clock boundary without all pinging in lockstep.
schema.py      SINGLE SOURCE OF TRUTH for the SQLite schema. each table's body columns are
               declared once; node DDL, hub DDL (+ node + src_id + UNIQUE for idempotent
               ingest), STD_TABLES and the generic insert()/insert_one() all derive from it.
               init_node()/init_hub() build the two DBs from the same spec.
events.py      edge-triggered ext_events emitters: trip/clear/edge/counter fire ONE row when a
               condition goes bad (and a quiet 'recovered' when it clears), so a stuck problem
               never re-floods the table or the wire. inputs are values a probe already computed.
governor.py    footprint back-off: when this process exceeds the RSS or DB-size budget, the
               scheduler sheds the costliest probes (ext/synthetic/mtr) for that cycle and logs
               a throttled event. both budgets default off, so default behaviour is unchanged.
```

## collection (node, stdlib-only)

```
collect.py     the unified collector daemon. `collect {fast|slow|all}` selects the probe set;
               fast = ping+net @10s, slow = http/mtr/wifi/host/ports (+ opt-in/auto probes).
               wraps each probe in _guarded() so the governor can shed it and a crash becomes an
               error event instead of killing the loop. owns scheduling; probes just sample.
probes/        one module per signal, each exposing collect(conn). full reference, what each
               does/refuses to do, and the shared footprint rules → probes/README.md.
adapters/      OS-specific reads behind one interface (read_net_counters / detect_tailscale_iface
               / wifi_probe). __init__.py dispatches on platform.system():
                 linux.py   /proc/net/dev counters, tailscale0, iw / /proc/net/wireless WiFi.
                 darwin.py  netstat byte counters, ifconfig tailscale, system_profiler WiFi.
```

## shipping + retention (node)

```
ship.py        push new rows (delta by ascending id) to the hub's /ingest. a node-local
               ship_state(table,last_id) cursor advances only on HTTP 200; the hub is idempotent
               (UNIQUE(node,src_id)) so optimistic advance is safe. gzips each batch once, reuses
               it across multiple hubs, and refuses plain-HTTP to a non-loopback/non-tailnet host
               so the secret never crosses the wire in clear. drain-once (timer) or loop.
expedite.py    out-of-band ship trigger: when a new elevated ext_events row lands, ship within
               ~10s instead of waiting for the bulk tick. one indexed MAX(id) read per check,
               at most one ship in flight, on a short-lived thread so a hung POST can't stall
               the collector loop.
prune.py       node-DB retention. deletes rows only when BOTH older than RETENTION_DAYS AND
               already shipped (when a hub is configured), so a hub outage backs up on disk
               rather than losing data. always wal_checkpoint(TRUNCATE)s; optional full VACUUM.
```

## hub (central, stdlib-only ingest + serving)

```
hub.py         the central ingest + web server (stdlib ThreadingHTTPServer). POST /ingest writes
               the hub DB in one idempotent transaction (INSERT OR IGNORE on UNIQUE(node,src_id));
               GET / serves the live fleet dashboard, /metrics prometheus, /health, and the
               read-only /api/* family. a writer connection (lock-guarded) + a separate read-only
               connection. runs rollup + alerts background passes.
hubapi.py      the read-only query layer behind those GET endpoints: prometheus/openmetrics
               exposition, the json api, fleet ranking, node×hour heatmap, service rollups.
               split from hub.py so it's unit-testable without a socket.
rollup.py      hub-side downsampling: periodically aggregates the heavy time-series into
               <table>_1m / <table>_1h tables so a days-long heatmap/ranking reads pre-aggregated
               buckets, not millions of raw rows. incremental + idempotent; the node is untouched.
alerts.py      hub-side service-alert DELIVERY: a background pass diffs the fleet's current
               degradations (the Risk-tab set) against an alert_state table and pushes newly
               firing/resolved alerts to SMOKEMON_NOTIFY_URL via notify.py (dedup / mute /
               re-notify cooldown). detection is reused from hubapi, not reinvented.
```

## analysis (hub-side, read-only — never imported by collectors)

```
analyze.py     the multi-signal engine: incident detection (isp-outage/link-down/packet-loss/
               latency-spike/dns-slow), correlation + blame (what deviated in the window + new
               procs), time-of-day anomaly baselines, change-point detection, mtr path
               intelligence, bandwidth attribution. pure stdlib over the shared query loaders.
mlanomaly.py   multivariate anomaly detection: scores each time bucket on how JOINTLY anomalous
               its signals are, catching correlated mild co-deviation (moderate cpu+temp+rtt =
               emerging thermal issue) that per-signal baselines miss. numpy path when present,
               stdlib robust-z fallback otherwise; every result carries its contributing signals.
```

## read + render (any DB; node or hub)

```
query.py       the shared read-side: window() + load_* loaders for both renderers. returns raw
               epoch timestamps, takes an optional --node filter (required on a multi-node hub
               DB). uses SQL LAG()/bare-column-with-MAX(ts) tricks where the SQLite version allows.
report.py      text surfaces on top of the analysis engine: `smoke status` (one-line sparkline),
               `smoke incidents` (incidents + blame), `smoke digest` (plain-english summary).
               renderer-free + stdlib, so they run on a bare node (no plotext/matplotlib import).
render/tui.py  the plotext text TUI: the 18 panel types on a configurable grid (auto 2-col on a
               wide terminal). a panel only draws if the node has that data.
render/png.py  the matplotlib/numpy PNG renderer: the same panel set, high-res, dated-daily
               output. logical per-panel width keeps individual samples distinguishable.
```

## surfaces / entrypoints

```
cli.py         the `smoke` command: one argparse entry point dispatching every subcommand
               (tui/live/kiosk/replay/fleet/png/daily/status/incidents/digest/footprint/hub).
               handles terminal capability (unicode vs ascii, colour) and the shared time flags.
notify.py      push/webhook alerting from detected incidents — ntfy / slack / discord / generic
               JSON, kind auto-detected from the URL. payload build is split from the send so it
               unit-tests without a socket; severity-gated so an all-clear window never alerts.
footprint.py   read-only collector-footprint estimator: rows produced in a window -> rows/day,
               and ship wire bytes by encoding the same compact JSON+gzip shape ship.py posts.
```

## the hard rules (what holds the package together)

`-` = a boundary the code must never cross; `+` = the invariant it keeps instead.

```diff
# architecture guardrails — enforced by import direction, not just convention
- collectors/ship/probes/adapters never import analyze/mlanomaly/rollup/alerts/hubapi/render
- the node never grows a heavy dependency: collection is stdlib + the external CLIs only
- no second copy of the schema: every table is declared once in schema.py and derived from
- no fabricated/seeded values anywhere — analysis reads only real collected rows
+ analysis + hub serving are hub-side and READ-ONLY over an already-populated DB
+ matplotlib/numpy (png) and plotext (tui) are read-side only; the edge never imports them
+ one schema feeds two DBs: node (raw) and hub (+ node + src_id + UNIQUE for idempotent ingest)
+ the daemons only collect/ship/serve; every read surface is a pure function of some DB
```

## entrypoints (launchd/systemd, or by hand with PYTHONPATH=repo)

```
python -m smokemon.collect {fast|slow|all}   the collector daemon (node)
python -m smokemon.probes.iperf              one iperf3 up+down sample (timer; real bandwidth)
python -m smokemon.probes.synthetic          one synthetic-checks sample (needs SYNTHETIC=1)
python -m smokemon.ship                       drain deltas to the hub(s)
python -m smokemon.prune                      delete shipped+old rows, shrink the WAL
python -m smokemon.hub                        ingest + read-api + dashboard server (hub)
python -m smokemon.notify                     alert on the last window's incidents (timer)
python -m smokemon.cli <sub>                  the `smoke` command (all read surfaces)
```

full env-var + deploy reference → [../INSTALL.md](../INSTALL.md) · what every metric means →
[../README.md](../README.md) · the probes in depth → [probes/README.md](probes/README.md).
