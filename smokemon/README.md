# smokemon/

> the python package. one codebase runs three roles — a **node** samples + detects + ships, a
> **hub** ingests + serves, and **read surfaces** (cli / text / json) reduce whatever's in a DB.
> everything is stdlib-only: node, shipper, hub and cli alike. a node stays at ~30 mb rss on a
> pi/jetson, and writes nothing to disk while things are normal.

every module is small and single-purpose. the split below is the whole architecture: a probe
samples, the detector decides, the scheduler runs the loop, the shipper pushes deltas, the hub
ingests and serves, and the read layer only ever *reduces* an already-populated DB. nothing here
daemonizes itself — systemd runs the entrypoints.

```
== data flow ==

node                                            hub
  collect (fast/slow) -> probes/                  POST /ingest (gzip delta, X-Smokemon-Key)
                          ^  adapters/ (os reads)       |
                          |                             v
     signals (in-memory ring, NEVER written)      hub -> smokemon-hub.db
                          |                             |  alerts (delivery only)
     detect + baseline -> incidents.py --\               v
     heartbeat --------------------------+-> smokemon.db
     events -> ext_events ---------------+        hubapi -> GET / · /metrics · /api/*
     expedite ---------------------------+--> ship ---^
     governor (sheds) / prune (retention)

read surfaces (any DB; node or hub):
  cli -> hubapi -> report      fleet / incidents / incident detail
  cli -> query  -> report      status / digest (text, stdlib)
  cli -> footprint             rows/day + ship bytes/day estimate
```

## runtime core (imported everywhere)

```
__init__.py    package marker + __version__ (the one place the version string lives).
config.py      all SMOKEMON_* env vars, node identity, paths, tool-path lookup. tri-state
               _enabled() (unset=auto, 0/off=disabled) backs the auto/opt-in probe gating.
               the single place config is read from the environment.
core.py        the daemon runtime shared by every collector: log(), connect() (WAL sqlite),
               install_signals(), run_scheduler() and a stable per-node _jitter() so a fleet
               fires on the same wall-clock boundary without all pinging in lockstep.
schema.py      SINGLE SOURCE OF TRUTH for the SQLite schema. each table's body columns are
               declared once; node DDL, hub DDL (+ node + src_id + UNIQUE for idempotent
               ingest), STD_TABLES and the generic insert()/insert_one() all derive from it.
               membership in _BODY is exactly "this table ships to the hub" — six tables:
               ext_events, device_facts, incidents, incident_samples, heartbeats,
               log_excerpts. node-local working state is declared by its owning module.
events.py      edge-triggered ext_events emitters: trip/clear/edge/counter fire ONE row when a
               condition goes bad (and a quiet 'recovered' when it clears), so a stuck problem
               never re-floods the table or the wire. inputs are values a probe already computed.
governor.py    footprint back-off: when this process exceeds the RSS or DB-size budget, the
               scheduler sheds the costliest probes for that cycle and logs a throttled event.
               both budgets default off, so default behaviour is unchanged.
```

## detection (node, stdlib-only — the core of the design)

```
signals.py     the bounded in-memory registry: the node's working memory, and the only place a
               sample lives while things are normal. three parallel array('d') per signal, so
               the ceiling is SIGNAL_MAX * SIGNAL_RING * 3 * 8 bytes (~74 KB) BY CONSTRUCTION,
               enforced in feed() — a node churning entity names cannot grow it. touches no
               SQLite, no shipping, no rules.
baseline.py    per-node learned normal: EWMA centre + EWMA absolute deviation as an online MAD
               surrogate. the decay constant is derived from the ACTUAL dt between samples
               (a = 1 - exp(-dt/tau)), so a 10 s signal and a 300 s signal learn at the same
               wall-clock rate. flushed on a timer, not per sample.
detect.py      the policy core: the rule table, the trip/clear thresholds, debounce, hysteresis,
               cooldown, and the OK/ARMED/OPEN/CLOSING/COOLDOWN state machine. two boundaries
               are enforced: it never touches SQLite (evaluate() returns Actions), and it never
               decides incident identity. every duration is on time.monotonic(), because pi and
               jetson nodes NTP-step at boot by hours.
incidents.py   persistence + identity: turns Actions into rows, mints the uid, owns the reopen
               policy and the transaction. writes one row per STATE TRANSITION, never an update
               in place — the shipper's monotonic cursor would make an update invisible to the
               hub forever.
heartbeat.py   agent liveness + the slow trends that only make sense as a continuous series
               (disk headroom, sd wear, the agent's own DB size). the only row a healthy node
               writes, and the reason the hub can tell "healthy" from "dead" once normal
               operation stops reaching disk. carries interval_s so staleness is derived from
               what the node does, not from hub-side config.
```

## collection (node, stdlib-only)

```
collect.py     the unified collector daemon. `collect {fast|slow|all}` selects the probe set;
               fast = ping+net @10s, slow = wifi+host+inventory+heartbeat (+ sweep and the
               baseline flush). wraps each probe in _guarded() so the governor can shed it and
               a crash becomes an error event instead of killing the loop.
probes/        one module per signal, each exposing collect(conn). probes sample and hand
               values to the detector; they decide nothing. full reference, what each does and
               refuses to do, and the shared footprint rules → probes/README.md.
adapters/      OS-specific reads behind one interface (read_net_counters / read_net_errors /
               detect_tailscale_iface / wifi_probe):
                 linux.py   /proc/net/dev counters, tailscale0, iw / /proc/net/wireless WiFi.
```

## shipping + retention (node)

```
ship.py        push new rows (delta by ascending id) to the hub's /ingest. a node-local
               ship_state(dest,table_name,last_id) cursor advances only on HTTP 200; the hub is
               idempotent (UNIQUE(node,src_id)) so optimistic advance is safe. gzips each batch
               once, reuses it across multiple hubs, and refuses plain-HTTP to a non-loopback
               host so the secret never crosses the wire in clear. drain-once (timer) or loop.
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
               GET / serves the fleet dashboard, /metrics prometheus, /health, and the read-only
               /api/* family. a writer connection (lock-guarded) + a separate read-only
               connection, plus a short-TTL response cache and the alerts background pass.
hubapi.py      the read-only query layer behind those GET endpoints: fleet liveness, the
               incident feed, incident detail with its evidence, node x hour incident density,
               events + excerpts, inventory, hub self-health, ship volume and the prometheus
               exposition. split from hub.py so it's unit-testable without a socket.
               static/dashboard.html is read from disk, not embedded here as a string.
alerts.py      hub-side alert DELIVERY only: a background pass projects the incidents the nodes
               have ALREADY opened (hubapi.open_incident_alerts) and pushes newly firing /
               resolved ones to SMOKEMON_NOTIFY_URL (dedup / mute / re-notify cooldown).
               detection is never repeated here — that would be a second, disagreeing opinion.
```

## read surfaces (any DB; node or hub)

```
query.py       the shared read-side: window(), load_incidents() (reduces the transition log per
               (node, uid)), load_incident_samples(), latest_heartbeat(), orphan_stats(). takes
               an optional --node filter (required on a multi-node hub DB).
report.py      the text surfaces: `smoke status` (one line), `smoke incidents` (the feed),
               `smoke incident UID` (detail + evidence), `smoke fleet`, `smoke fleet --heatmap`
               (incident density), `smoke digest`. stdlib, so they run on a bare node.
analyze.py     hub-side shaping of incidents the nodes ALREADY detected: correlation into
               clusters, target classification, severity ranking and the robust statistics
               those need. it never re-derives an incident — the continuous series that would
               take is not stored. read-only, never imported by collectors/ship/probes.
footprint.py   read-only collector-footprint estimator: rows produced in a window -> rows/day,
               and ship wire bytes by encoding the same compact JSON+gzip shape ship.py posts.
```

## surfaces / entrypoints

```
cli.py         the `smoke` command: one argparse entry point dispatching every subcommand
               (fleet/incidents/incident/status/digest/footprint/hub). the incident views read
               a hub DB, a node DB, or the hub over --hub-url — all three go through the same
               hubapi functions, so every transport hands the renderer the same shape.
notify.py      push/webhook alerting from opened incidents — ntfy / slack / discord / generic
               JSON, kind auto-detected from the URL. payload build is split from the send so it
               unit-tests without a socket; severity-gated so an all-clear window never alerts.
```

## the hard rules (what holds the package together)

`-` = a boundary the code must never cross; `+` = the invariant it keeps instead.

```diff
# architecture guardrails — enforced by import direction, not just convention
- normal operation is never written to disk; only confirmed transitions + their evidence are
- detect.py never touches SQLite, and never decides incident identity — incidents.py owns both
- collectors/ship/probes/adapters never import analyze/hubapi/alerts
- the node never grows a dependency: the whole node-side list is fping, iw and python3 stdlib
- no second copy of the schema: every table is declared once in schema.py and derived from
- no fabricated/seeded values anywhere — every surface reads only real collected rows
+ absence of a `close` is NEVER read as proof a fault persists: an open incident on a node with
+ a stale heartbeat reports as `unknown`, not `ongoing`
+ the hub only DELIVERS alerts; the node already did debounce, hysteresis, cooldown and dedup
+ one schema feeds two DBs: node (raw) and hub (+ node + src_id + UNIQUE for idempotent ingest)
+ the daemons only sample/detect/ship/serve; every read surface is a pure function of some DB
```

## entrypoints (systemd, or by hand with PYTHONPATH=repo)

```
python -m smokemon.collect {fast|slow|all}   the collector + detector daemon (node)
python -m smokemon.ship                       drain deltas to the hub(s)
python -m smokemon.prune                      delete shipped+old rows, shrink the WAL
python -m smokemon.hub                        ingest + read-api + dashboard server (hub)
python -m smokemon.notify                     alert on the last window's incidents (timer)
python -m smokemon.cli <sub>                  the `smoke` command (all read surfaces)
```

full env-var + deploy reference → [../INSTALL.md](../INSTALL.md) · what every signal means →
[../README.md](../README.md) · the probes in depth → [probes/README.md](probes/README.md) ·
the rule table + state machine → [../docs/detector-spec.md](../docs/detector-spec.md).
