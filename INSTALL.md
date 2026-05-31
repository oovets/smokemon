# smokemon — install & reference

full install and operations reference for smokemon. the short version lives in
[README.md](README.md); this is the detailed one. smokemon is a `smokemon/` python package: collectors, shipper and hub are stdlib-only, the renderers add plotext (TUI) or matplotlib+numpy (PNG). a node runs two long-lived collector daemons (`collect fast` = ping+net @10s, `collect slow` = http+mtr+wifi+host @60s/30s) plus three timers (`iperf` @15min, `ship` @60s, `prune` daily). a hub runs one process (`smokemon.hub`) that ingests delta batches the nodes push to it. everything is driven by launchd (macOS) or systemd (Linux); nothing daemonizes itself.

```
one node (local only)

  collect fast (ping+net)  --\
  collect slow (http/mtr/   --|-> data/smokemon.db -> smoke tui / smoke png
               wifi/host)   --|
  iperf (timer)            --/

multi-node + central hub (push model, tailscale-friendly)

  node1: collect+iperf -> smokemon.db -> ship --\
  node2: collect+iperf -> smokemon.db -> ship ---} POST /ingest  (X-Smokemon-Key)
  node3: collect+iperf -> smokemon.db -> ship --/
                                                 v
                              smokemon.hub  (:8765, ThreadingHTTPServer)
                                                 v
                                    data/smokemon-hub.db
                                                 v
            smoke fleet                (whole fleet on one screen, stdlib)
            smoke png/tui --node NAME  (drill into a single node's panels)
            GET / · /metrics · /api/*  (web dashboard, prometheus, json)

package layout

  smokemon/ config core schema collect ship hub query cli
            adapters/{darwin,linux}
            probes/{ping,net,http,mtr,wifi,iperf,host}
            render/{tui,png}
  deploy/   launchd/*.plist   systemd/*
  install.sh   (repo root; works local or curl-piped)
```

the package itself is documented file-by-file in [smokemon/README.md](smokemon/README.md) (node /
hub / read-surface split, what every module does, the import-direction guardrails). each probe is
documented in [smokemon/probes/README.md](smokemon/probes/README.md): what it measures, what it
deliberately refuses to do, and the edge-footprint rules they all share.

schema is single-source (`schema.py`): node DDL, hub DDL (adds `node` + `src_id` +
`UNIQUE(node,src_id)`), `STD_TABLES` and the generic INSERT all derive from one table
spec. migrations are additive (`ensure_node_column`), so the node DB and the hub DB share
one schema and one plotter codebase. storage is SQLite WAL; a daily prune
(`python -m smokemon.prune`, enabled by `install.sh`) deletes node rows older than
`SMOKEMON_RETENTION_DAYS` (default 14) once they have been shipped, then checkpoint-truncates
the WAL so the file actually shrinks. raw growth before pruning is ~5–6 GB/yr per node, so a
14-day window keeps a node DB small; set `SMOKEMON_RETENTION_DAYS=0` to keep everything (and
roll up to a lower resolution rather than deleting raw data blindly). footprint is ~30 MB RSS
per node (two daemons) and well under 1% of one core; the hub adds ~20 MB.

```
node:  python3 >=3.10 (stdlib only for collection); plotext for the local TUI.
       macOS:  brew install fping mtr iperf3   (curl/netstat/ifconfig/system_profiler built in)
       Linux:  apt install fping mtr-tiny iperf3 iw

hub:   python3 >=3.10 + matplotlib + numpy (PNG) + iperf3 (runs iperf3 -s as a bandwidth
       target so nodes can test throughput to the hub).

net:   prefer Tailscale/VPN between node and hub. The hub is plain HTTP (no TLS) on
       8765/tcp — bind it to a private address only.
```

macos single host (test run, no launchd):

```
git clone <repo> ~/smokemon && cd ~/smokemon
brew install fping mtr iperf3 && python3 -m pip install --user plotext
PYTHONPATH=. python3 -m smokemon.collect all &      # all probes in one process
PYTHONPATH=. python3 -m smokemon.cli live 6h        # live TUI, last 6h
```

linux single host (one-liner; clones to /opt/smokemon, installs systemd units):

```
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --node "$(hostname)"
# (no --hub-url => local only, nothing shipped)
```

multi-node + central hub:

```
# hub (once):
curl -fsSL .../install.sh | sudo bash -s -- --hub --secret MY_SECRET
# each node:
curl -fsSL .../install.sh | sudo bash -s -- --node NAME \
    --hub-url http://HUB-HOST:8765/ingest --secret MY_SECRET
# on the hub, watch the whole fleet on one screen (no --node, defaults to the hub DB):
PYTHONPATH=/opt/smokemon python3 -m smokemon.cli fleet live
# or from any terminal, over HTTP, with no DB access:
PYTHONPATH=/opt/smokemon python3 -m smokemon.cli fleet --hub-url http://HUB-HOST:8765
# drill into a single node's panels:
PYTHONPATH=/opt/smokemon python3 -m smokemon.cli png \
    --db /opt/smokemon/data/smokemon-hub.db --node NAME --hours 24
```

fan-out to several hubs (redundancy): set `SMOKEMON_HUB_URLS` in the node env file to a
semicolon-separated list of `/ingest` URLs, or run `smoke hub HUB-A HUB-B`. every hub receives a
complete copy; one that is down just backs up on the node's disk and catches up when it returns.
a local row is pruned once **at least one** hub has confirmed it. each batch is gzipped once and
reused across hubs (CPU stays ~1x; only egress scales with hub count). per-hub secrets are optional
and positional via `SMOKEMON_HUB_SECRETS` (an empty slot = the shared `SMOKEMON_HUB_SECRET`). a
single hub still uses `SMOKEMON_HUB_URL` and behaves exactly as before.

the plists run `python3 -m smokemon.*` with `WorkingDirectory` + `PYTHONPATH` set to the
repo, and include a `PATH` with `/opt/homebrew/{bin,sbin}` so `shutil.which` finds
fping/mtr/iperf3. no pip install required.

```
brew install fping mtr iperf3 && python3 -m pip install --user plotext
git clone <repo> ~/smokemon && cd ~/smokemon && mkdir -p data logs

# mtr needs root -> passwordless sudo for the exact binary:
sudo tee /etc/sudoers.d/smokemon >/dev/null <<EOF
$(whoami) ALL=(root) NOPASSWD: $(command -v mtr)
EOF

# the plists are templates: replace /Users/YOUR_USERNAME with your home dir (and the
# python path if not /usr/bin/python3) before bootstrapping.
sed -i '' "s#/Users/YOUR_USERNAME#$HOME#g" deploy/launchd/*.plist
cp deploy/launchd/*.plist ~/Library/LaunchAgents/
for s in collect-fast collect-slow iperf daily prune; do
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.smokemon.$s.plist
done
# optional shipper (edit SMOKEMON_HUB_URL + SMOKEMON_HUB_SECRET first) and hub:
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.smokemon.{shipper,hub}.plist
```

services: `collect-fast` (RunAtLoad+KeepAlive), `collect-slow` (KeepAlive), `iperf`
(StartInterval 900s), `daily` (`smoke daily` at 23:55), `prune` (daily at 00:20, DB
retention), `shipper` (StartInterval 60s, optional), `hub` (KeepAlive, optional).

`install.sh` does it all: apt deps, `setcap cap_net_raw+ep` on fping/mtr-packet (so mtr
needs no sudo), `pip --user plotext`, writes `/etc/smokemon.env`, installs `smoke` (plus
`smokelive`/`smokekiosk`/`smokepng`) as executables in `/usr/local/bin` — on PATH in every
shell immediately, no relogin — and template-substitutes + enables the systemd units.

```
sudo ./install.sh --node NAME [--hub-url http://HUB-HOST:8765/ingest --secret S] [--targets a,b]
# or piped, see Quickstart. Units enabled:
#   smokemon-collect-fast.service   ping/net, always on
#   smokemon-collect-slow.service   http/mtr/wifi/host, always on
#   smokemon-iperf.timer            iperf every 15 min (targets the hub when --hub-url is set)
#   smokemon-shipper.timer          ship every 60s
#   smokemon-prune.timer            DB retention: prune shipped+old rows daily
```

```
# Linux:
sudo ./install.sh --hub --secret SHARED_SECRET
#   apt: iperf3 + python3-matplotlib + python3-numpy; writes /etc/smokemon.env
#   (SMOKEMON_HUB_DB, HUB_BIND=0.0.0.0, HUB_PORT=8765, HUB_SECRET); enables smokemon-hub
#   + smokemon-iperf-server (iperf3 -s on :5201, the bandwidth target nodes test against).

# macOS: use deploy/launchd/com.smokemon.hub.plist (set SMOKEMON_HUB_SECRET, bind a
# private address), then launchctl bootstrap it.
```

the hub listens on 8765/tcp. writes go to `POST /ingest` (header `X-Smokemon-Key`); reads
are open and unauthenticated: `GET /` (live fleet dashboard), `GET /health`, `GET /metrics`
(prometheus/openmetrics), and the read-only json family `GET /api/{nodes,latest,fleet,
fleet-status,heatmap,risks,cost,services,logs,ports,network,inventory,ingest-rate,spark}`
plus `GET /api/{plot,png}?node=NAME` (render a node's panels server-side). `services` is the
docker/redis/pipeline fleet rollup behind the dashboard's services tab; `logs` backs the logs
tab. if the secret is the default `changeme` it logs a warning at startup and refuses ingest.

> [!WARNING]
> the hub speaks plain HTTP — no TLS — and the read endpoints (`GET /`, `/metrics`, `/api/*`) have
> no auth. expose 8765 only over a private network (Tailscale / VPN / LAN), never the public
> internet. ingest is gated by the shared secret (`X-Smokemon-Key`) and refuses to start on the
> default `changeme`.

set in the launchd plist `EnvironmentVariables` (macOS) or `/etc/smokemon.env` (Linux).

<details markdown="1">
<summary>all SMOKEMON_* environment variables (click to expand)</summary>

```
general
  SMOKEMON_DB              local SQLite DB        (default <repo>/data/smokemon.db)
  SMOKEMON_NODE            node name              (default hostname)

ping + net (collect fast)
  SMOKEMON_TARGETS         comma-sep ping targets (default 1.1.1.1,gw). the token gw
                           (or gateway/auto) auto-detects this node's default gateway, so a
                           fresh install needs no per-site LAN address; drop it if undetected.
  SMOKEMON_INTERVAL        seconds/cycle          (default 10)
  SMOKEMON_COUNT           pings/cycle/target     (default 20)
  SMOKEMON_PERIOD          ms between pings        (default 50)
  SMOKEMON_FPING           fping path             (fallback: PATH lookup)

http + mtr + wifi (collect slow)
  SMOKEMON_PROBE_INTERVAL  seconds/cycle          (default 60)
  SMOKEMON_HTTP_URLS       comma-sep URLs         (default google.com, cloudflare.com)
  SMOKEMON_MTR_TARGETS     comma-sep mtr targets  (default 1.1.1.1)
  SMOKEMON_MTR_COUNT       pings/mtr run          (default 10)
  SMOKEMON_MTR_SUDO        1 = sudo -n mtr, 0 = direct (set 0 on Linux after setcap)
  SMOKEMON_WIFI            1/0 enable WiFi probe  (default 1)
  SMOKEMON_CURL, SMOKEMON_MTR   tool paths        (fallback: PATH lookup)

host (collect slow)
  SMOKEMON_HOST_INTERVAL   seconds/sample         (default 30)
  SMOKEMON_PROC_TOPN       top-CPU procs kept     (default 5)
  SMOKEMON_THROTTLE_TEMP   degC throttle ceiling for the temp death-clock (default 80)

iperf (probes.iperf)
  SMOKEMON_IPERF_SERVER    iperf3 -s host         (install.sh defaults it to the --hub-url
                                                  host; unset -> probe no-ops)
  SMOKEMON_IPERF_DURATION  seconds/direction      (default 5)
  SMOKEMON_IPERF           iperf3 path            (fallback: PATH lookup)

synthetic transactions (probes.synthetic, opt-in)
  SMOKEMON_SYNTHETIC       1 = enable captive-portal + DoH checks (default 0/off)
  SMOKEMON_DOH_URL         DNS-over-HTTPS endpoint (default https://cloudflare-dns.com/dns-query)
  SMOKEMON_DOH_NAME        name to resolve via DoH (default example.com)
  SMOKEMON_CAPTIVE_URL     204-no-content probe URL
                           (default http://connectivitycheck.gstatic.com/generate_204)

external lightweight scrapes (probes.ext, opt-in)
  SMOKEMON_EXT_HTTP        ; separated endpoints:
                           name=url[|kind=json|metrics][|metrics=a,b,c]
                           always stores up + latency_ms; JSON stores numeric fields,
                           OpenMetrics requires an explicit metrics allowlist.
                           example: app=http://127.0.0.1:8080/health
  SMOKEMON_EXT_INTERVAL    seconds/cycle          (default 300)
  SMOKEMON_EXT_TIMEOUT     seconds/request        (default 2)
  SMOKEMON_EXT_MAX_BYTES   max response bytes     (default 256 KiB)
  SMOKEMON_EXT_MAX_METRICS max parsed metrics/source/cycle (default 20)
                           no log streaming, Docker scans, or journal tails on edge.

redis stream health (probes.redisq, auto, stdlib socket/RESP; no redis-cli)
  auto by default: samples only if a Redis is reachable, silent no-op otherwise.
  SMOKEMON_REDIS           0 = disable; 1 = force (record a down row even if unreachable)
  SMOKEMON_REDIS_HOST      host                    (default 127.0.0.1)
  SMOKEMON_REDIS_PORT      port                    (default 6379)
  SMOKEMON_REDIS_TIMEOUT   seconds/request         (default 1)
  SMOKEMON_REDIS_INTERVAL  seconds/cycle           (default 60)
  SMOKEMON_REDIS_STREAMS   comma-separated streams for XLEN
  SMOKEMON_REDIS_GROUPS    ; separated stream=group pairs for XPENDING
                           server row also records connected/blocked clients, ops/sec,
                           evicted_keys and rejected_connections from one INFO call.

docker container health (probes.dockerps, auto, stdlib unix-socket HTTP; no docker CLI)
  auto by default: samples only when the docker socket exists, silent no-op otherwise.
  SMOKEMON_DOCKER          0 = disable; 1 = force (record daemon-down even if socket absent)
  SMOKEMON_DOCKER_SOCK     engine socket           (default /var/run/docker.sock)
  SMOKEMON_DOCKER_API      engine API version      (default v1.41)
  SMOKEMON_DOCKER_INTERVAL seconds/cycle           (default 60)
  SMOKEMON_DOCKER_TIMEOUT  seconds/request         (default 2)
  SMOKEMON_DOCKER_MAX_BYTES max response bytes     (default 512 KiB)
  SMOKEMON_DOCKER_MAX      max containers/cycle    (default 60)
  SMOKEMON_DOCKER_INSPECT  1 = add restart_count/exit_code/oom via inspect (default 1)
  SMOKEMON_DOCKER_CGROUP   1 = add per-container cpu/mem from cgroup v2 sysfs (default 1)
                           one bounded GET per cycle; no `docker logs`, no log/journal tails.

pipeline / process liveness (probes.pipeline, auto, stdlib /proc + RTSP socket)
  auto by default: watches any running gst-launch process and probes every rtsp:// URL
  found inside those cmdlines (e.g. rtspclientsink location=...) with no config at all.
  SMOKEMON_PIPELINE        0 = disable entirely (default on)
  SMOKEMON_PIPELINE_AUTO   0 = only use the explicit lists below, no gst/rtsp auto-detection
  SMOKEMON_PROC_WATCH      ; separated label=substring pairs matched against /proc cmdlines
                           example: gst=gst-launch-1.0;app=python app.py
                           reports count, cpu/rss, youngest-process uptime, restart count.
  SMOKEMON_RTSP_URLS       ; separated label=rtsp://... (or bare urls); one OPTIONS each
                           example: cam=rtsp://127.0.0.1:8554/imx519
  SMOKEMON_PIPELINE_INTERVAL seconds/cycle         (default 60)
  SMOKEMON_RTSP_TIMEOUT    seconds/request         (default 2)

alerting (notify, S4)
  SMOKEMON_NOTIFY_URL      ntfy / slack / discord / webhook URL (unset -> no alerts)
  SMOKEMON_NOTIFY_KIND     ntfy|slack|discord|generic ("" = auto-detect from host)
  SMOKEMON_NOTIFY_MIN_SEVERITY  min incident severity to alert on (1-3, default 2)

ship (push -> hub)   (repoint a node any time with `smoke hub NEW-HUB`)
  SMOKEMON_HUB_URL         hub /ingest URL        (unset -> ship no-ops)
  SMOKEMON_HUB_SECRET      shared secret          (default changeme - CHANGE)
  SMOKEMON_HUB_INSECURE    1 = allow plain-HTTP shipping to a non-loopback host (default 0:
                           the shipper refuses unless the URL is https or the host is loopback,
                           so the secret never crosses the wire in clear — set 1 on a trusted
                           LAN/VPN where the documented plain-HTTP hub is reachable)
  SMOKEMON_SHIP_BATCH      max rows/batch/table   (default 2000)
  SMOKEMON_SHIP_INTERVAL   in-process loop seconds; 0 = drain once and exit (default 0). the
                           "@60s" cadence is the systemd/launchd timer, not this loop — the
                           timer re-runs `python -m smokemon.ship` (which drains and exits)
                           every 60s. set >0 only to run the shipper as its own daemon.
  SMOKEMON_SHIP_EXPEDITE   1 = on (default): an elevated event kicks an immediate ship so
                           errors reach the hub in seconds, rate-limited by SHIP_EXPEDITE_INTERVAL
  SMOKEMON_SHIP_EXPEDITE_INTERVAL  min seconds between expedited ships (default 10)
  SMOKEMON_SHIP_RTTS       1 = also ship raw per-ping rtts (default 0). off keeps the raw
                           rtts node-local: the hub renders percentile bands from the
                           aggregates in ping_runs, so this cuts ~85% of ship traffic for
                           no hub-side change. ingest bodies are gzipped either way.
  SMOKEMON_SHIP_EXCLUDE    comma-sep extra table names to NOT ship. the rows are still collected
                           and kept node-local; they are just excluded from the push. this ADDS to
                           a baked-in default (synthetic_samples, which no hub surface reads), so
                           your own additions never silently re-enable a known dead-weight table.
                           backward compatible: the hub simply receives fewer table keys.
  SMOKEMON_SHIP_INCLUDE    comma-sep table names to force-ship even if defaulted-out. e.g.
                           SMOKEMON_SHIP_INCLUDE=synthetic_samples to ship the synthetic checks
                           after all (only useful once a hub-side consumer for them exists).
  SMOKEMON_SHIP_PROC       proc_samples ship mode: active (default) | all | self. the node records
                           the top-N processes by cpu + its own 'smokemon' row every host cycle, but
                           the hub only reads the 'smokemon' row (footprint + ship cost) and, at
                           incident time, the busy processes. active ships 'smokemon' + procs whose
                           cpu_pct >= SMOKEMON_SHIP_PROC_MIN_CPU and drops idle top-N rows (kept
                           node-local); all ships every proc row (prior behaviour); self ships only
                           the 'smokemon' row. dropped rows are never re-examined (cursor advances).
  SMOKEMON_SHIP_PROC_MIN_CPU  cpu_pct floor for active mode (default 5.0)

retention / prune (node DB; `python -m smokemon.prune`, daily timer)
  SMOKEMON_RETENTION_DAYS  delete rows older than N days (default 14); 0 = keep everything.
                           when a hub is configured, a row is deleted only once it is BOTH
                           older than N days AND shipped, so a hub outage never loses data.
  SMOKEMON_PRUNE_VACUUM    1 = also run a full VACUUM after pruning (heavier, reclaims pages
                           to the filesystem; default 0 — freed pages are reused by new inserts)

footprint governor (node, opt-in; sheds expensive probes when over budget)
  SMOKEMON_MAX_RSS_MB      this process's RSS ceiling in MB (0 = disabled, the default)
  SMOKEMON_MAX_DB_MB       node DB (+WAL) size ceiling in MB (0 = disabled)
                           over budget -> drops mtr/synthetic/ext for that cycle, logs an event.

inventory (device facts, auto, delta-coded; near-zero steady-state cost)
  SMOKEMON_INVENTORY       0 = disable (default on); emits a device_facts row only on change
  SMOKEMON_INVENTORY_INTERVAL  scan seconds (default 3600)

log excerpts (opt-in, OFF by default; capped+redacted tail, never a stream)
  SMOKEMON_LOGEXCERPT      1 = enable shipping a tail of LOGEXCERPT_PATHS on warn/error events
  SMOKEMON_LOGEXCERPT_PATHS  comma-sep files to tail
  SMOKEMON_LOGEXCERPT_MAX_BYTES  per-excerpt hard cap (default 16 KiB)
  SMOKEMON_LOGEXCERPT_ALWAYS  1 = capture every cycle regardless of events (testing)

hub (smokemon.hub)
  SMOKEMON_HUB_DB          hub DB path            (default <home>/smokemon/data/smokemon-hub.db)
  SMOKEMON_HUB_BIND        listen address         (default 0.0.0.0)
  SMOKEMON_HUB_PORT        port                   (default 8765)
  SMOKEMON_HUB_SECRET      shared secret          (must match the nodes)
  SMOKEMON_HUB_MAX_BODY    max POST bytes         (default 64 MiB)
  SMOKEMON_HUB_CACHE_TTL_S short-TTL cache for the heavy aggregate endpoints (default 20; 0=off)
  SMOKEMON_HUB_LATEST_WINDOW_S  bound the latest-row lookup as the DB grows (default 30 days; 0=off)
  SMOKEMON_AWS_GB_COST     $/GB applied to each node's ship volume for the cost tab (default 0.09)

hub alert delivery (smokemon.hub background pass; tracks always, pages if NOTIFY_URL set)
  SMOKEMON_ALERT_TRACK     0 = disable the background pass (default on: tracks firing alerts)
  SMOKEMON_ALERT_EVAL_INTERVAL  seconds between passes (default 60)
  SMOKEMON_ALERT_RENOTIFY_S  re-page a still-firing alert after this many seconds (default 1800)
  SMOKEMON_ALERT_NOTIFY_RESOLVED  1 = also page when an alert clears (default 1)
  SMOKEMON_ALERT_MUTE      ; list of node/kind/label globs never paged (still shown on the Risk tab)
```

</details>

run as `smoke <sub>` (zsh helper) or `python -m smokemon.cli <sub>` (`PYTHONPATH`=repo).
default sub is `tui`.

there are three families of commands: **panel views** of one host (graphs, need
plotext/matplotlib), **fleet views** of every node at once (hub-wide, stdlib-only), and
**text analysis** (stdlib-only, runs on a node too). all read a DB; nothing collects.

```
shared time/scope flags (panel + text views):
  --db PATH                local DB by default; point at the hub DB to read shipped data
  --hours N | --minutes N | --since ISO --until ISO     window (default last 6h)
  --targets a,b,c          limit ping/mtr targets
  --panels ping,net,http,mtr,wifi,iperf,host,gpu,redis,docker,pipeline,disk,
           thermal,power,tcp,psi,freq,self | all  (a panel only draws if the node has its data)
  --node NAME              pick one node — REQUIRED on a hub DB (every node's rows are mixed)
  --cols N                 grid columns (0 = auto: 2 if wide enough and >=3 panels)

(1) panel views — one host, graphed (plotext for tui, matplotlib+numpy for png)
  smoke [tui]          static TUI                            (+ --kiosk, --reserve N)
  smoke live [win]     redraw in place; win = Nh/Nm/number(min); --refresh N (10); --bell
  smoke kiosk [win]    live + clean (no legend/axes/header, minimal title); --bell on degraded
  smoke replay [when]  DVR scrubber over a window (date / datetime / Nh); ←/→ scrub ↑/↓ step q
  smoke png            matplotlib PNG; --out, --width INCHES (0=auto), --dpi N (96), --no-open
  smoke daily          dated 24h PNG -> graphs/daily/smokemon[-NODE]-YYYY-MM-DD.png
  # view a single node from the hub:  smoke tui --db .../smokemon-hub.db --node NAME

(2) fleet views — every node at once (hub-wide, stdlib-only, the terminal twin of GET /)
  smoke fleet          worst-first status table, one line/node: state · RTT · loss · cpu · temp
  smoke fleet live     same, repainting in place;   --refresh N (5) · --bell on any down/stale
  smoke fleet --ranked incident ranking: uptime% · RTT · incidents · downtime over --hours
  smoke fleet --heatmap [--metric loss|rtt]   node × hour sparkline grid over --hours
  fleet-only flags:
    --db PATH          hub DB (default SMOKEMON_HUB_DB) — no --node needed, shows all nodes
    --hub-url URL      read the hub's read-only /api over HTTP instead (e.g. http://HUB:8765);
                       no DB file access required, so it works from any terminal
    --hours N (24) · --stale-after S (300, fresh-sample cutoff) · --no-color

(3) text analysis — stdlib only, runs on a node too (uses the shared flags above)
  smoke status         one-line sparkline health summary (internet/wifi/cpu + verdict)
  smoke incidents      detected incidents + multi-signal blame
  smoke digest         plain-english window summary
                       incidents/digest take --notify -> push qualifying incidents to SMOKEMON_NOTIFY_URL

(4) collector footprint — stdlib only, read-only
  smoke footprint      rows produced by collectors, estimated rows/day, SQLite bytes/day,
                       and the current shipper JSON+gzip bytes/day estimate
    --db PATH          node DB by default; use --node NAME when reading a hub DB
    --hours N (24) · --minutes N · --since/--until · --ship-rtts · --limit N

node config
  smoke hub            show where this node ships + hub reachability
  smoke hub HOST       repoint it: writes SMOKEMON_HUB_URL (host -> http://host:8765/ingest)
                       to /etc/smokemon.env; the shipper picks it up on its next run (<=15s).
                       (root-owned file -> prints the sudo line if smoke can't write it;
                       macOS keeps the value in the launchd plist, so it prints that path.)

daemons (launchd/systemd, or by hand with PYTHONPATH=repo) — these collect/ship/serve:
  python -m smokemon.collect fast | slow | all   ping+net / http+mtr+wifi+host / both
  python -m smokemon.probes.iperf      one iperf3 up+down sample
  python -m smokemon.probes.synthetic  one synthetic-checks sample (needs SMOKEMON_SYNTHETIC=1)
  python -m smokemon.ship              drain deltas to the hub
  python -m smokemon.prune             delete shipped rows older than retention, shrink the WAL
  python -m smokemon.hub               run the ingest + read-API + dashboard server
  python -m smokemon.notify            alert on the last hour's incidents (for a timer)
```

every table has a `node` column (default `SMOKEMON_NODE`/hostname). the hub DB additionally
has `src_id` + `UNIQUE(node, src_id)` per table for idempotent ingest.

```
ping_runs      one row per fping cycle (target, time, counts, loss%, rtt aggregates)
ping_rtts      every individual RTT (run_id -> ping_runs.id)
net_samples    cumulative byte counters per interface, per cycle
http_samples   DNS / connect / TLS / TTFB / total ms per URL
mtr_hops       per-hop loss / avg / best / worst / stddev
wifi_samples   RSSI / noise / tx-rate / channel
iperf_samples  up + down Mbit/s + retransmits
host_samples   CPU% / load / mem% / temp / disk IO / PSI / swap / freq / throttle
disk_samples   used% + free GB per mount
proc_samples   top-N processes by CPU (plus a `smokemon` self-footprint row)
thermal_zones  per-zone temperature             power_samples  per-rail watts/volts/amps
tcp_samples    retrans / RSTs / udp errors / conntrack fill
disk_health    SD/eMMC wear-level (hourly)
synthetic_samples  captive-portal + DoH check results (probe/ok/latency/detail)
ship_state     (node DB only) shipper cursor per table
```

```
services   macOS: launchctl list | grep smokemon      Linux: systemctl status 'smokemon-*'
logs       macOS: ~/smokemon/logs/*.{out,err}.log      Linux: journalctl -u smokemon-collect-fast -f
reload     macOS: launchctl bootout/bootstrap gui/$(id -u) <plist>   (wait out the old one)
           Linux: systemctl restart smokemon-collect-fast
ship now   Linux: sudo systemctl start smokemon-shipper.service

hub up?    curl -s http://HUB:8765/health         (-> {"ok": true, ...})
           dashboard: open http://HUB:8765/ in a browser; metrics: GET /metrics
           (401 = up, 200 = accepted, 413 = body too big)   or: ss -ltnp | grep 8765

no mtr/iperf on macOS   the launchd PATH must include /opt/homebrew/{bin,sbin} (the plists
                        set it) so shutil.which finds the Homebrew binaries.
no mtr on Linux         getcap "$(command -v mtr-packet)" should show cap_net_raw+ep; set
                        SMOKEMON_MTR_SUDO=0 in /etc/smokemon.env.
no wifi on Linux        `iw dev` must list a wireless iface and /proc/net/wireless be non-empty.
db growth               daily prune keeps ~SMOKEMON_RETENTION_DAYS (14) of rows; raw rate is
                        ~5-6 GB/yr. RETENTION_DAYS=0 keeps everything — then aggregate to a
                        lower resolution rather than deleting raw data blindly.
```

```
macOS
  for p in ~/Library/LaunchAgents/com.smokemon.*.plist; do
      launchctl bootout gui/$(id -u) "$p"; rm -f "$p"
  done                                  # data/ and logs/ remain

Linux
  sudo systemctl disable --now smokemon-collect-fast smokemon-collect-slow \
      smokemon-shipper.timer smokemon-iperf.timer smokemon-hub \
      smokemon-iperf-server 2>/dev/null
  sudo rm -f /etc/systemd/system/smokemon-*.{service,timer} /etc/smokemon.env
  sudo systemctl daemon-reload          # data/ remains in the repo dir
```
