# Installing Smokemon

> **The short version lives in [README.md](README.md). This is the complete one.**

Smokemon is a `smokemon/` Python package — stdlib only, everywhere. Node, shipper, hub and CLI alike. No pip install, no virtualenv, no build step.

A **node** runs two collector daemons and two timers. A **hub** runs one process. Everything is driven by systemd; nothing daemonizes itself.

The storage model is the thing to understand first:

> **Nothing is written while things are normal.**

Probes hand their values to the detector, which keeps them in a bounded in-memory ring and writes only when a rule confirms an anomaly. A quiet node writes 288 heartbeat rows per day and nothing else.

See [docs/adr/0001-incident-pivot.md](docs/adr/0001-incident-pivot.md) for why, and [docs/detector-spec.md](docs/detector-spec.md) for the rule table, the state machine and the baseline.

---

# Requirements

| | |
|---|---|
| **Node** | Python ≥ 3.10 (stdlib only) · `apt install fping iw` |
| **Hub** | Python ≥ 3.10 · nothing else to install |
| **Network** | Tailscale / VPN / LAN between node and hub |

> [!WARNING]
> The hub speaks plain HTTP — no TLS — and its read endpoints have no auth. Bind 8765/tcp to a private address only, never the public internet.

---

# Install

**Try it on one host, no systemd:**

```bash
git clone <repo> ~/smokemon && cd ~/smokemon
sudo apt install fping iw
PYTHONPATH=. python3 -m smokemon.collect all &   # every probe in one process
PYTHONPATH=. python3 -m smokemon.cli status      # health summary
PYTHONPATH=. python3 -m smokemon.cli incidents   # what has broken
```

**Single host, properly (clones to /opt/smokemon, installs systemd units):**

```bash
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --node "$(hostname)"
# no --hub-url => local only, nothing shipped
```

**A fleet with a central hub:**

```bash
# on the hub, once:
curl -fsSL .../install.sh | sudo bash -s -- --hub --secret MY_SECRET

# on each node:
curl -fsSL .../install.sh | sudo bash -s -- --node NAME \
    --hub-url http://HUB-HOST:8765/ingest --secret MY_SECRET

# then watch the whole fleet on one screen:
PYTHONPATH=/opt/smokemon python3 -m smokemon.cli fleet live
```

`install.sh` does everything: apt deps, `setcap cap_net_raw+ep` on fping (so ping needs no sudo), writes `/etc/smokemon.env`, installs `smoke` into `/usr/local/bin` (on PATH immediately, no relogin), and enables the systemd units.

With no `--secret` on the hub it generates a strong random one and prints it.

**Units enabled:**

```text
smokemon-collect-fast.service   ping + net, always on
smokemon-collect-slow.service   wifi + host + inventory + heartbeat, always on
smokemon-shipper.timer          ship every 15s
smokemon-prune.timer            delete shipped + expired rows, daily
smokemon-hub.service            (hub only) ingest + read API + dashboard
```

---

# How it fits together

```text
one node (local only)

  collect fast (ping+net)    ──┐
                               ├──► detector ──► in-memory ring (never written)
  collect slow (wifi/host/   ──┘        │
               inventory)               └──► data/smokemon.db
                                              incidents + evidence + heartbeat,
                                              and nothing else

multi-node + central hub (push model)

  node1: collect ──► smokemon.db ──► ship ──┐
  node2: collect ──► smokemon.db ──► ship ──┼──► POST /ingest  (X-Smokemon-Key)
  node3: collect ──► smokemon.db ──► ship ──┘
                                              │
                                    smokemon.hub  (:8765)
                                              │
                                    data/smokemon-hub.db
                                              │
                     smoke fleet          whole fleet, one screen
                     smoke incidents      fleet-wide incident feed
                     GET / · /metrics · /api/*
```

**Package layout:**

```text
smokemon/  config core schema signals baseline detect incidents heartbeat
           collect ship hub hubapi query report cli
           adapters/linux
           probes/{ping,net,host,wifi,inventory,logexcerpt}
           static/dashboard.html
deploy/    systemd/*
install.sh (repo root; works local or curl-piped)
```

Every module is documented file-by-file in [smokemon/README.md](smokemon/README.md). Every probe — what it measures and what it deliberately refuses to do — in [smokemon/probes/README.md](smokemon/probes/README.md).

---

# Fan-out to several hubs

Set `SMOKEMON_HUB_URLS` to a semicolon-separated list of `/ingest` URLs, or run `smoke hub HUB-A HUB-B`.

Every hub receives a complete copy. One that is down just backs up on the node's disk and catches up when it returns. A local row is pruned once **at least one** hub has confirmed it.

Each batch is gzipped once and reused across hubs, so CPU stays ~1× and only egress scales with hub count. Per-hub secrets are optional and positional via `SMOKEMON_HUB_SECRETS` (an empty slot falls back to the shared secret).

---

# Configuration

Everything is set in `/etc/smokemon.env`. The defaults are chosen so a fresh install needs no tuning.

<details markdown="1">
<summary><b>All SMOKEMON_* environment variables</b> (click to expand)</summary>

```text
general
  SMOKEMON_DB              local SQLite DB        (default <home>/smokemon/data/smokemon.db)
  SMOKEMON_NODE            node name              (default hostname)
  SMOKEMON_ENV_FILE        config file `smoke hub` reads/writes (default /etc/smokemon.env)

ping + net (collect fast)
  SMOKEMON_TARGETS         comma-sep ping targets (default 1.1.1.1,gw). the token gw
                           (or gateway/auto) auto-detects this node's default gateway, so a
                           fresh install needs no per-site LAN address; drop it if undetected.
  SMOKEMON_INTERVAL        seconds/cycle          (default 10)
  SMOKEMON_COUNT           pings/cycle/target     (default 20)
  SMOKEMON_PERIOD          ms between pings       (default 50)
  SMOKEMON_FPING           fping path             (fallback: PATH lookup)

wifi + host (collect slow)
  SMOKEMON_PROBE_INTERVAL  seconds/cycle          (default 60)
  SMOKEMON_WIFI            1/0 enable WiFi probe  (default 1)
  SMOKEMON_HOST_INTERVAL   seconds/sample         (default 30)
  SMOKEMON_THROTTLE_TEMP   degC throttle ceiling; detect.RULES derives host.temp's trip/clear
                           from it, so a different SoC moves both by moving this one number
                           (default 80)

detection — signal registry, baseline, rules and incidents
  ** every var below is specified in full in docs/detector-spec.md — defaults only here **
  SMOKEMON_SIGNAL_MAX      max distinct signals held in memory            (default 48)
  SMOKEMON_SIGNAL_RING     samples kept per signal                        (default 64)
  SMOKEMON_SIGNAL_STALE_S  silent-signal timeout -> incident closed stale (default 600)
  SMOKEMON_BASELINE_TAU_S  EWMA decay, wall-clock seconds                 (default 86400)
  SMOKEMON_BASELINE_GATE_Z winsorising threshold                          (default 4.0)
  SMOKEMON_BASELINE_MAX_N  warmup counter saturation                      (default 100000)
  SMOKEMON_BASELINE_FLUSH_S  seconds between baseline flushes to disk     (default 900)
  SMOKEMON_RULES           sparse per-field rule overrides; example:
                             SMOKEMON_RULES='ping.loss:trip=15,for_s=30;host.temp:trip=75'
  SMOKEMON_INCIDENT_PRE    pre-incident evidence samples kept             (default 6)
  SMOKEMON_INCIDENT_POST   recovery-tail samples kept                     (default 3)
  SMOKEMON_INCIDENT_DURING_MAX       max mid-incident samples             (default 24)
  SMOKEMON_INCIDENT_DURING_HEAD      samples at native cadence first      (default 6)
  SMOKEMON_INCIDENT_DURING_STEP0     first back-off step, seconds         (default 60)
  SMOKEMON_INCIDENT_DURING_GROWTH    back-off growth factor               (default 2.0)
  SMOKEMON_INCIDENT_DURING_STEP_MAX  back-off ceiling, seconds            (default 3600)
  SMOKEMON_INCIDENT_MAX_OPEN         concurrent incidents kept in full    (default 16)
  SMOKEMON_INCIDENT_MAX_OPEN_S       force-close an incident this old     (default 86400)
  SMOKEMON_INCIDENT_REOPEN_WINDOW_S  re-trip continues the same uid       (default 900)
  SMOKEMON_DETECT_DRYRUN   1 = detect for real but LOG instead of writing (default 0).
                           nothing reaches disk, so it is safe to leave on; use it to learn
                           a node's real incident rate before committing to thresholds.
  SMOKEMON_HEARTBEAT_INTERVAL  seconds/row (default 300 -> 288 rows/day)

alerting
  SMOKEMON_NOTIFY_URL      ntfy / slack / discord / webhook URL (unset -> no alerts)
  SMOKEMON_NOTIFY_KIND     ntfy|slack|discord|generic ("" = auto-detect from host)
  SMOKEMON_NOTIFY_MIN_SEVERITY  min incident severity to alert on (1-3, default 2)

ship (push -> hub)   (repoint a node any time with `smoke hub NEW-HUB`)
  SMOKEMON_HUB_URL         hub /ingest URL        (unset -> ship no-ops)
  SMOKEMON_HUB_URLS        ; separated list of /ingest URLs for fan-out (supersedes HUB_URL)
  SMOKEMON_HUB_SECRET      shared secret          (default changeme - CHANGE)
  SMOKEMON_HUB_SECRETS     ; separated per-hub secrets, positional; empty slot = HUB_SECRET
  SMOKEMON_HUB_INSECURE    1 = allow plain-HTTP shipping to a non-loopback host (default 0:
                           the shipper refuses unless the URL is https or the host is loopback,
                           so the secret never crosses the wire in clear — set 1 on a trusted
                           LAN/VPN where the documented plain-HTTP hub is reachable)
  SMOKEMON_SHIP_BATCH      max rows/batch/table   (default 2000)
  SMOKEMON_SHIP_INTERVAL   in-process loop seconds; 0 = drain once and exit (default 0). the
                           "@15s" cadence is the systemd timer, not this loop. set >0 only to
                           run the shipper as its own daemon.
  SMOKEMON_SHIP_EXPEDITE   1 = on (default): an elevated event kicks an immediate ship so
                           errors reach the hub in seconds
  SMOKEMON_SHIP_EXPEDITE_INTERVAL  min seconds between expedited ships (default 10)
  SMOKEMON_SHIP_EXCLUDE    comma-sep tables to NOT ship (still collected, kept node-local)
  SMOKEMON_SHIP_INCLUDE    comma-sep tables to force-ship even if excluded by default

retention / prune (node DB; `python -m smokemon.prune`, daily timer)
  SMOKEMON_RETENTION_DAYS  delete rows older than N days (default 14); 0 = keep everything.
                           when a hub is configured, a row is deleted only once it is BOTH
                           older than N days AND shipped, so a hub outage never loses data.
  SMOKEMON_PRUNE_VACUUM    1 = also run a full VACUUM after pruning (heavier, reclaims pages
                           to the filesystem; default 0 — freed pages are reused by new inserts)

footprint governor (node, opt-in)
  SMOKEMON_MAX_RSS_MB      this process's RSS ceiling in MB (0 = disabled, the default)
  SMOKEMON_MAX_DB_MB       node DB (+WAL) size ceiling in MB (0 = disabled)
                           over budget -> sheds logexcerpt, then inventory, for that cycle and
                           logs a throttled event. ping/net/host/heartbeat and the detector
                           sweep are never shed: a node under pressure is exactly when you
                           need it to still be watching.

inventory (device facts, auto, delta-coded; near-zero steady-state cost)
  SMOKEMON_INVENTORY       0 = disable (default on); emits a device_facts row only on change
  SMOKEMON_INVENTORY_INTERVAL  scan seconds (default 3600)

log excerpts (opt-in, OFF by default; capped+redacted tail, never a stream)
  SMOKEMON_LOGEXCERPT      1 = enable shipping a tail of LOGEXCERPT_PATHS on warn/error events
  SMOKEMON_LOGEXCERPT_PATHS  comma-sep files to tail
  SMOKEMON_LOGEXCERPT_INTERVAL  seconds/cycle     (default 60)
  SMOKEMON_LOGEXCERPT_MAX_BYTES  per-excerpt hard cap (default 16 KiB)
  SMOKEMON_LOGEXCERPT_ALWAYS  1 = capture every cycle regardless of events (testing)

hub (smokemon.hub)
  SMOKEMON_HUB_DB          hub DB path            (default <home>/smokemon/data/smokemon-hub.db)
  SMOKEMON_HUB_BIND        listen address         (default 0.0.0.0)
  SMOKEMON_HUB_PORT        port                   (default 8765)
  SMOKEMON_HUB_SECRET      shared secret          (must match the nodes)
  SMOKEMON_HUB_MAX_BODY    max POST bytes         (default 64 MiB)
  SMOKEMON_HUB_CACHE_TTL_S short-TTL cache for the aggregate endpoints (default 20; 0=off)

hub alert delivery (background pass; tracks always, pages if NOTIFY_URL set)
  SMOKEMON_ALERT_TRACK     0 = disable the background pass (default on: tracks firing alerts)
  SMOKEMON_ALERT_EVAL_INTERVAL  seconds between passes (default 60)
  SMOKEMON_ALERT_RENOTIFY_S  re-page a still-firing alert after this many seconds (default 1800)
  SMOKEMON_ALERT_NOTIFY_RESOLVED  1 = also page when an alert clears (default 1)
  SMOKEMON_ALERT_MUTE      ; list of fnmatch globs matched against the alert key (the incident
                           uid). a matched alert is never paged; it still shows in the dashboard.
```

</details>

---

# The CLI

Run as `smoke <sub>` (installed by `install.sh`) or `python -m smokemon.cli <sub>` with `PYTHONPATH` set to the repo. The default sub is `status`, so bare `smoke` works.

Every surface is read-only and stdlib, so all of them run on a node as well as on the hub. The incident views read a hub DB or a node DB — both hold the same `incidents` table — and default to the hub DB when one exists.

**Incidents — the primary surface**

```text
smoke incidents      what broke, where, when, and whether it is still broken
  --db PATH          default: the hub DB if present, else the node DB
  --hub-url URL      read /api over HTTP instead (e.g. http://HUB:8765)
  --hours N (24) · --node NAME · --no-color

smoke incident UID   one incident in full: its transitions, the pre/during/post
                     evidence samples, and any log excerpt linked to it
  --db PATH · --hub-url URL · --no-color
```

**Fleet views — every node at once (the terminal twin of `GET /`)**

```text
smoke fleet             worst-first, one line/node: state · liveness · open incidents
smoke fleet live        same, repainting in place;  --refresh N (5) · --bell
smoke fleet --heatmap   node × hour incident-density grid over --hours (default 168)
  --db PATH             hub DB (default SMOKEMON_HUB_DB) — no --node needed
  --hub-url URL         read the hub's /api over HTTP; no DB file access required
```

**Text summaries**

```text
smoke status      one-line health summary: what is open now + heartbeat age
smoke digest      window summary: counts by severity, longest incidents, slow trends
                  --notify -> push qualifying incidents to SMOKEMON_NOTIFY_URL
```

**Collector footprint**

```text
smoke footprint   rows produced, estimated rows/day, SQLite bytes/day, and the
                  current shipper JSON+gzip bytes/day estimate
  --db PATH · --hours N (24) · --limit N (8)
```

**Node config**

```text
smoke hub           show where this node ships + hub reachability
smoke hub HOST      repoint it: writes SMOKEMON_HUB_URL to /etc/smokemon.env;
                    the shipper picks it up on its next run (<=15s)
smoke hub A B ...   fan out to several hubs (writes SMOKEMON_HUB_URLS)
```

Shared time/scope flags on `status` / `digest` / `footprint`: `--db PATH`, `--hours N` | `--minutes N` | `--since ISO --until ISO` (default last 6h), and `--node NAME` — **required** on a hub DB, where every node's rows are mixed.

**Daemons** (systemd, or by hand with `PYTHONPATH=repo`):

```text
python -m smokemon.collect fast | slow | all
python -m smokemon.ship      drain deltas to the hub(s)
python -m smokemon.prune     delete shipped+expired rows, shrink the WAL
python -m smokemon.hub       ingest + read API + dashboard
python -m smokemon.notify    alert on the last hour's incidents (for a timer)
```

---

# The hub API

The hub listens on 8765/tcp. Writes go to `POST /ingest` with the `X-Smokemon-Key` header. Reads are open and unauthenticated:

| Endpoint | What |
|---|---|
| `GET /` | fleet dashboard (a static HTML asset) |
| `GET /health` | `{"ok": true, ...}` |
| `GET /metrics` | prometheus / openmetrics |
| `GET /api/*` | `nodes` `fleet` `incidents` `incident` `density` `logs` `inventory` `hub-health` `cost` `ingest-rate` |

`incident` takes `?uid=`. `density` returns the node × hour incident-count grid. `logs` backs the dashboard's logs tab (ext_events + captured log excerpts).

If the secret is still the default `changeme`, the hub logs a warning at startup and refuses ingest.

> [!NOTE]
> `/metrics` exports liveness, open-incident counts, heartbeat age and orphan-sample count — there are no per-signal gauges. The node no longer ships a time series, and synthesising one here from incident windows would export a chart made only of the bad moments.

---

# What reaches disk

Six shipped tables, and that is the whole of it. Every table has a `node` column; the hub DB additionally has `src_id` + `UNIQUE(node, src_id)` per table for idempotent ingest.

```text
incidents         one row per STATE TRANSITION (open|reopen|close|stale|expired|persistent),
                  never updated in place — the hub reduces rows per (node, uid). threshold,
                  baseline, mad and z are stored AS EVALUATED, so an incident stays
                  interpretable after a rule change.
incident_samples  the evidence window: phase = pre (from the in-memory ring at trip time) |
                  during (decimated) | post (recovery tail). joined on (node, uid).
heartbeats        liveness + slow trends + detector self-observation. the only row a
                  healthy node writes.
ext_events        edge-triggered agent events (governor sheds, probe crashes, db contention)
device_facts      delta-coded inventory: a row only when a fact actually changes
log_excerpts      opt-in capped + redacted log tails, captured on an event, never a stream

ship_state        (node DB only) shipper cursor per hub, per table
```

Node-local working state (`incident_state`, `signal_baseline`, `log_cursors`) is declared by its owning module rather than in `schema._BODY`, so the shipper never sees it.

Schema is single-source in `schema.py`: node DDL, hub DDL, `STD_TABLES` and the generic INSERT all derive from one table spec. Migrations are additive, so the node DB and the hub DB share one schema.

Storage is SQLite WAL. The daily prune deletes node rows older than `SMOKEMON_RETENTION_DAYS` once they have been shipped, then checkpoint-truncates the WAL so the file actually shrinks.

Footprint is roughly **30 MB RSS per node** (two daemons) and well under 1% of one core. The hub adds ~20 MB.

---

# Operating it

```text
services   systemctl status 'smokemon-*'
logs       journalctl -u smokemon-collect-fast -f
reload     systemctl restart smokemon-collect-fast
ship now   sudo systemctl start smokemon-shipper.service
hub up?    curl -s http://HUB:8765/health      or: ss -ltnp | grep 8765
```

**Troubleshooting**

| Symptom | Check |
|---|---|
| No ping data | `getcap "$(command -v fping)"` should show `cap_net_raw+ep` |
| No wifi | `iw dev` must list a wireless iface, and `/proc/net/wireless` be non-empty |
| Nothing in the DB | That is the expected state for a healthy node. Check `smoke status` for a fresh heartbeat; run with `SMOKEMON_DETECT_DRYRUN=1` to see what the detector *would* have written |
| Node shows `unknown` | An incident is open but the heartbeat has gone stale — the hub cannot tell whether the fault persists. Check the node itself |
| DB growth | The daily prune keeps ~14 days. A quiet node writes only heartbeats, so growth tracks incident rate, not uptime |

---

# Uninstall

```bash
sudo systemctl disable --now smokemon-collect-fast smokemon-collect-slow \
    smokemon-shipper.timer smokemon-prune.timer smokemon-hub 2>/dev/null
sudo rm -f /etc/systemd/system/smokemon-*.{service,timer} /etc/smokemon.env
sudo rm -f /usr/local/bin/smoke /usr/local/bin/smokeincidents
sudo systemctl daemon-reload    # data/ remains in the repo dir
```
