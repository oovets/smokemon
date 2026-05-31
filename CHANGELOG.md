# smokemon - changelog

all notable changes to smokemon. the format roughly follows [keep a changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

roadmap / ideas -> [PLAN.md](PLAN.md)

(0.1.0 through 0.10.0 landed together during the initial build-out and were not separately
tagged; dated entries begin at the first release, 0.11.0.)

```
== unreleased ==

added:

- ship: stop shipping tables the hub does not consume, while still collecting and keeping them
  node-local. synthetic_samples (DoH/captive-portal checks) has no hub-side reader, so it is now
  excluded from the push BY DEFAULT - shipping and storing it hub-side was dead weight. backward
  compatible: the hub just receives fewer table keys (the ingest contract never required any
  specific table). SMOKEMON_SHIP_EXCLUDE (comma-separated) ADDS more tables to the default
  exclusion; SMOKEMON_SHIP_INCLUDE force-ships a defaulted-out table (e.g. to re-enable
  synthetic_samples once a hub-side consumer for it exists).

- hub storage: downsampling/rollups (smokemon/rollup.py, hub-side, pure stdlib) wired into the
  heavy read paths. the hub aggregates the bulky time-series tables (ping_runs/host_samples/
  net_samples/tcp_samples/wifi_samples) into additive <table>_1m and <table>_1h tables, driven by
  a rollup_state cursor and run incrementally from the hub's hourly housekeeping pass (only
  fully-closed buckets; the open bucket is left until it closes; idempotent via
  UNIQUE(node,entity,bucket_ts)). query._resolution() picks raw/1m/1h by window span; fleet(),
  risks() and heatmap() read rollups for long windows (>6h) and raw for short ones, falling back
  to raw when a rollup has no rows in range. measured on a 30-day / 15-node synthetic hub over a
  7-day window: risks() 3.6s -> 0.67s, fleet() 1.27s -> 0.28s, heatmap() 0.47s -> 0.07s. the node
  is untouched (still ships raw 10s data); short-window views and node-local use keep full
  fidelity. a scripts/bench_hub_reads.py harness reproduces the numbers (and runs against a real
  hub DB via --db). scores
  each time bucket on how jointly anomalous its signals are, so a cluster of mild co-deviations
  (moderate cpu + temp + a little rtt drift = an emerging thermal issue) surfaces even when no
  single signal trips an incident - the payoff of the synchronized timeline. uses a numpy
  mahalanobis distance when numpy is present (already the png extra) and a pure-stdlib root-sum-
  square of co-deviating robust-z values otherwise, so it stays importable on a bare node. every
  result names its contributing signals (never an opaque score). surfaced as a new "anomalies"
  tier in the risk tab + per-node risk modal and a line in `smoke digest`.

- analysis: incident correlation / storm dedup (analyze.correlate_incidents). co-firing incidents
  on a node (one root cause tripping loss + latency + a restart at once) fold into a single group
  with a likely root, exposed as incident_groups on /api/risks; raw members ride along so a genuine
  second fault is never hidden.

changed:

- death clocks: disk-full and SD-wear ETAs now project with the robust theil-sen slope (median of
  pairwise slopes, query.theil_sen_eta_seconds) instead of least squares, so a single cache flush
  or log rotation no longer skews the ETA and the clocks stop flapping. pure stdlib; linear_eta_
  seconds is kept for back-compat.
```

```
== 0.16.0 - 2026-05-31  log/error visibility + per-application network view ==

added:

- hub: a "logs" dashboard tab + /api/logs - a fleet-wide, newest-first stream of ext_events
  (warn/error/crit) and log_excerpts, filterable by node (the filter box) and severity
  (all/elevated/error), with tail-truncated excerpts. Surfaces data that already ships; no new
  per-device capture.

- events: edge/delta-triggered ext_events producers riding on data the probes already collect
  (no new I/O / footprint), so the logs tab gets real warn/error/crit signal: collector
  probe-crash (error, then a quiet recover), host OOM-kill (crit), CPU thermal-throttle (warn),
  over-temp (crit), swap >90% (warn), Pi under-voltage (crit) / throttled (warn), and a per-URL
  HTTP 5xx/no-response (warn) for the built-in checks. Each fires once on transition (a stuck
  condition never re-floods the table or the wire) and clears quietly when it recovers.

- ship: expedite-on-error. When an elevated ext_events row lands, the collector kicks an
  out-of-band ship on a daemon thread (~10s detection on the fast loop, rate-limited + coalesced)
  so errors reach the hub in seconds, decoupled from the bulk ship cadence. Toggle via
  SMOKEMON_SHIP_EXPEDITE / SMOKEMON_SHIP_EXPEDITE_INTERVAL. ext_events/log_excerpts are also
  gathered before the bulk metric tables so they lead any backlog batch.

- hub: a "network" dashboard tab + /api/network - per-application throughput (bytes/s) over time
  from port_samples, as positive deltas of the bucketed cumulative byte gauge. Fleet-wide by
  default (each app summed across nodes); drill into one node via the filter box. Well-known ports
  map to service names (https/redis/ssh/postgres/…) for labels.

fixed:

- collector: a transient SQLite "database is locked" (WAL writer contention on a busy node, not a
  probe bug) is now surfaced once as an edge-triggered warn (db-contention) instead of a per-cycle
  error, and expedite ignores collector-sourced events - closing a crash -> expedite-ship ->
  more-contention -> crash feedback loop that flapped ping on multi-writer nodes. Event recording
  is itself best-effort (a locked DB while logging the event no longer cascades).

changed:

- hub: /api/ports is now cached (was an uncached MAX(ts) scan + full fetch on every modal open),
  so the per-node ports tab no longer lags when opened.

== 0.15.0 - 2026-05-31  multi-hub fan-out shipping ==

added:

- ship: fan-out to multiple hubs. Set SMOKEMON_HUB_URLS to a semicolon-separated list of /ingest
  URLs (or `smoke hub HUB-A HUB-B`) and every hub receives a complete copy - no single point of
  failure on the receiving side. A single SMOKEMON_HUB_URL behaves exactly as before. Per-hub
  secrets are optional and positional via SMOKEMON_HUB_SECRETS (empty slot = shared HUB_SECRET).

- ship: each batch is gathered and gzipped ONCE per shared cursor frontier and the same body is
  POSTed to every hub at that frontier, so node CPU stays ~1x regardless of hub count; only egress
  scales. A hub that lagged behind (was unreachable) gets its own catch-up gather.

- ship: per-destination delivery is isolated - a hub that is down keeps its own cursor untouched
  and never blocks or rolls back shipping to the others; it catches up on its backlog when it
  returns (the hub stays idempotent on UNIQUE(node, src_id), so replays are dropped).

- prune: with multiple hubs, a local row is deletable once AT LEAST ONE configured hub has
  confirmed it (MAX cursor across destinations). A configured-but-unreachable hub (cursor 0)
  never blocks pruning of data another hub already took.

- cli: `smoke hub` lists all configured targets with per-hub reachability; `smoke hub HOST...`
  takes several hosts to set up fan-out (writes SMOKEMON_HUB_URLS) and clears the single-var form
  so the two can't shadow each other.

- hub: serve a favicon (the header sparkline, brand-blue on the dashboard's dark tile) at
  /favicon.svg, linked from the dashboard <head>, with /favicon.ico aliased to it - no more 404
  for the tab icon.

- dashboard: first-open warm-up screen on every tab. The cache-backed tabs (ranking / heatmap /
  risks / cost / services) show it while the hub builds their cache; grid / table show it until
  the first /api/fleet-status poll lands. Instead of a grey blank the view shows a spinner and
  explains that the first open reads history / warms the cache, then stays instant. Shown only
  on the initial empty view - returning to a tab refreshes in place with no flash.

changed:

- ship_state migrates in place from a single per-table cursor (table_name PK) to a per-destination
  composite key (dest, table_name); old cursors are remapped to the primary hub. Migration is
  atomic and idempotent. Note: downgrading to pre-0.15 code on a migrated DB requires dropping
  ship_state (cursors reset; re-ship is harmless thanks to hub idempotency).

== 0.14.1 - 2026-05-31  post-deploy hotfixes ==

fixed:

- ship: allow http over Tailscale addresses (100.64.0.0/10 + IPv6 ULA fd7a:115c:a1e0::/48).
  The 0.14.0 transport guard refused them and broke the existing tailnet fleet; the tailnet is
  WireGuard-encrypted, so the shared secret is not exposed. No SMOKEMON_HUB_INSECURE needed.

- hub: tolerate a client that hangs up mid-response. A dashboard reload / fetch abort closes the
  socket; the writer then raised BrokenPipe/ConnectionReset, and the generic 500 handler tried to
  write to the dead socket too -> an unhandled traceback per cancelled request in the hub log.
  Response writes now swallow client-disconnect errors (GET and POST), so these are silent.

== 0.14.0 - 2026-05-31  edge hardening: retention, governor, transport, inventory ==

added:

- prune (smokemon/prune.py; `python -m smokemon.prune`, daily timer/plist): node-DB
  retention. deletes rows older than SMOKEMON_RETENTION_DAYS (default 14) but only once
  they are shipped (id <= the ship_state cursor) when a hub is set, so a hub outage backs
  up on disk instead of losing data; age-only when no hub. orphaned ping_rtts are dropped
  with their parent runs. truncates the WAL afterward (SMOKEMON_PRUNE_VACUUM=1 adds a full
  VACUUM). without this the append-only tables grew forever and wore out SD cards.

- governor (smokemon/governor.py; opt-in, off by default): when the collector exceeds
  SMOKEMON_MAX_RSS_MB or SMOKEMON_MAX_DB_MB it sheds its costliest probes (ext/synthetic/
  mtr) for that cycle and writes a throttled ext_events row, so detail degrades gracefully
  under pressure instead of the footprint overrunning target. ping/net/host always run.

- probes.inventory (auto-on, vslow, delta-coded): device/environment facts (model, kernel,
  os release, jetpack/l4t, cpu/mem, interfaces, gateway, boot id) into a new additive
  device_facts table. a row is written only when a fact changes, so steady-state cost is one
  /proc+/sys scan per hour that usually emits nothing. SMOKEMON_INVENTORY=0 disables.

- probes.logexcerpt (opt-in, off by default): event-driven capped log tails into a new
  log_excerpts table - NOT log streaming. ships a redacted last-N-KB excerpt of the files in
  SMOKEMON_LOGEXCERPT_PATHS only when a warn/error+ ext_events row just appeared (governor
  sheds, probe anomalies). per-file byte-offset cursor (never resends), drop-oldest byte cap
  (SMOKEMON_LOGEXCERPT_MAX_BYTES), secret redaction; the shipper gzips the wire. seeded at
  probe start so enabling never dumps history. SMOKEMON_LOGEXCERPT_ALWAYS=1 captures always.

- hub GET /api/inventory: per-node device_facts (latest value per key, grouped by hw/os/net/
  runtime) so the inventory the nodes collect is surfaced fleet-wide.

changed:

- self panel now reports the honest multi-daemon footprint: rss_mb on the name='smokemon'
  proc_samples row is summed across ALL smokemon pids (fast + slow + transient shipper), not
  just one process, and a new write_mb_day column projects the fleet's SD-write rate so card
  wear is as visible as RSS (TUI/PNG titles show "NN MB/day SD"). linux /proc only.

- cadence now carries a stable per-node jitter (hash of node name -> 0..interval/4 offset) so
  the fleet no longer pings/ships in wall-clock lockstep - spreads simultaneous POSTs and net
  probes across the window.

- hub serves dashboard/API GETs through a dedicated read-only connection (own lock, WAL) so
  reads run concurrently with ingest instead of queuing behind the writer's lock.

performance (fixes intermittent dashboard "fetch error / NetworkError" on a large hub DB):

- hub sample tables now also get a plain (ts) index and a per-entity (node, <entity>, ts) index,
  not just (node, ts). cross-node `WHERE ts >= ?` windows (heatmap/spark/cost/services) seek the
  (ts) index instead of full-scanning, and the latest-value queries (latest_metrics, /metrics,
  services) become a loose index scan that jumps to each group's newest row instead of scanning
  all history + a temp b-tree. created on next hub start (one-time index build on existing DBs).

- latest_metrics is bounded to SMOKEMON_HUB_LATEST_WINDOW_S (default 30d; 0 = unbounded) so the
  live/latest surfaces only consider recent rows; a node silent longer drops out of "latest".

- init_hub runs a bounded PRAGMA optimize (analysis_limit=400) so the planner has the stats to
  pick the new loose-index scans.

note: these are read-path/index changes only - no sample data is deleted. the hub DB itself is
still not pruned (it keeps full history for replay/baselines); bound its growth with a hub-side
retention pass if/when needed.

security:

- ship now refuses to send when SMOKEMON_HUB_URL is plaintext http:// to a non-loopback
  host (the shared X-Smokemon-Key would cross the wire in clear). allow https, loopback, a
  Tailscale address (100.64.0.0/10 or the IPv6 ULA fd7a:115c:a1e0::/48 - the tailnet is
  WireGuard-encrypted, and is the project's actual fleet path), or SMOKEMON_HUB_INSECURE=1
  for another trusted LAN.

- log excerpts are secret-redacted (Bearer/Basic creds, key=value pairs, the hub secret
  verbatim) before they are ever written to the DB.

== 0.13.0 - 2026-05-31  docker + pipeline collectors, redis enrichment ==

added (all three run by default and self-detect their dependency, staying a cheap no-op on
nodes that don't run the corresponding service; each can be disabled with SMOKEMON_*=0):

- probes.dockerps: container health via one bounded HTTP GET over the docker unix socket
  per slow cycle (stdlib socket + manual HTTP/1.0, no docker CLI, no `docker logs`, no
  log/journal tails). auto-samples only when the socket exists. records state/running,
  health, exit_code, restart_count, oom_killed; optional per-container cpu/mem/pids from
  cgroup-v2 sysfs and an optional small per-container inspect. new docker_samples table;
  status/digest surface unhealthy / non-zero-exit / restarting containers, or a
  daemon-down row (SMOKEMON_DOCKER=1 forces this even with the socket absent).

- probes.pipeline: process + stream liveness. with zero config it auto-watches any running
  gst-launch process and probes every rtsp:// URL found inside those cmdlines (e.g.
  rtspclientsink location=...). one /proc scan reports count, summed cpu/rss, youngest
  process uptime and a cumulative restart count (flips when the youngest starttime
  changes); a bounded RTSP OPTIONS confirms a stream is actually being served. explicit
  SMOKEMON_PROC_WATCH / SMOKEMON_RTSP_URLS add to this; SMOKEMON_PIPELINE_AUTO=0 disables
  detection. new proc_watch + stream_probes tables. pure stdlib, no ps/ffprobe, no tails.

- probes.redisq: auto-samples only when a Redis is reachable (silent no-op otherwise;
  SMOKEMON_REDIS=1 forces a down row when unreachable). the server row now also records
  connected_clients, blocked_clients, instantaneous ops/sec, evicted_keys and
  rejected_connections, parsed from INFO clients/stats on the existing connection.

- dashboard + renderers: two new time-series panels (docker = per-container cpu/mem, or a
  running-count fallback when cgroup stats are off; pipeline = watched-process cpu + RTSP
  latency) render in both the PNG and TUI paths and surface automatically in the hub's
  per-node modal. the redis panel now overlays ops/sec. new hub /api/services endpoint +
  a "services" dashboard tab: a fleet-wide table of docker containers (bad/daemon-down
  flagged), redis instances (mem/clients/ops + hottest streams), watched processes and
  stream probes, each row click-through to the node graphs. query.load_docker/load_pipeline
  feed the panels; load_redis carries the enriched server series. all read-only, stdlib +
  the existing optional matplotlib/plotext render deps.

== 0.12.0 - 2026-05-30  analysis engine + text surfaces + alerting + hub api ==

added:

- analysis engine smokemon/analyze.py (roadmap F1/F2/P1/P2/P3/X5): hub-side,
  read-only, pure-stdlib. derives everything from already-collected metrics; never
  imported by collectors/ship/probes.

  - F2 incident detection: contiguous loss/latency runs over the ping series classified
    as isp-outage / upstream-loss / link-down (internet-down vs gateway-clean
    disambiguation), packet-loss, latency-spike, dns-slow (dns phase dominant while tcp
    connect stays fast). minor transient loss is filtered (a non-total run must peak >=10%).

  - F1 multi-signal blame: for each incident window, every signal (cpu, mem, swap, temp,
    psi, wifi rssi/retry, tcp retrans, bandwidth, cpu clock) that deviates from its
    out-of-window baseline by >=3 robust-sigma (MAD), plus processes that appeared during
    the window, ranked by deviation.

  - P1 time-of-day baseline: per-(weekday, hour) median+MAD baseline + robust-z anomaly
    flag - "abnormal for a tuesday 14:00" with no thresholds, no ml.

  - P2 change-point detection: variance-weighted recursive mean-shift split, catches silent
    regime changes (an isp speed-tier drop, a permanent route change).

  - P3 mtr path intelligence: route-change detection (a hop whose resolved host changed),
    worst-hop attribution, path-stability score, read per-sample from mtr_hops.

  - X5 bandwidth attribution: bandwidth spikes (robust-z) cross-referenced with the top-cpu
    processes during each spike - explicitly heuristic (proc_samples has no per-process byte
    counters).

- text surfaces smokemon/report.py, renderer-free (stdlib, run on a node too):

  - smoke status (QW3): one-line unicode-sparkline health summary - internet rtt/loss, wifi
    rssi, cpu temp, and a verdict word.

  - smoke incidents (F1/F2): incident table with per-incident multi-signal blame.

  - smoke digest (F3): plain-english window summary - uptime %, incident class breakdown,
    honest hard-downtime (outage spans unioned, never summed), peak latency + what it
    coincided with, bufferbloat grade, wifi roams, thermals.

- smoke replay (S1): dvr-style scrubber over any historical window - a sliding playhead
  frame moved with left/right (vim h/l), step size on up/down, q to quit. raw data is kept
  forever, so any past window is reachable.

- push/webhook alerting smokemon/notify.py (S4): fire incident alerts to ntfy, a slack or
  discord incoming webhook, or a generic json endpoint (kind auto-detected from the url).
  severity-gated via SMOKEMON_NOTIFY_MIN_SEVERITY; --notify on smoke incidents/digest, plus
  a smokemon-notify timer entry point. pure urllib.

- hub http surfaces smokemon/hubapi.py on the existing hub server:

  - GET / : live fleet dashboard (self-contained html, no external assets) - polls
    /api/fleet-status and renders a dense, worst-first, colour-coded one-line-per-node grid.

  - GET /metrics (S2): prometheus/openmetrics exposition of the latest per-node gauges
    (ping rtt/loss, cpu, mem, temp).

  - GET /api/{nodes,latest,fleet,heatmap,fleet-status} (S3): read-only json - fleet ranking
    (uptime %, median rtt, incident count, downtime), a node x hour loss/rtt heatmap, and a
    fast latest-sample fleet-status (healthy/warn/down/stale) for the dashboard.

- smoke fleet: terminal twin of the GET / dashboard - an aggregated, worst-first view of
  every node reporting to the hub, pure-stdlib (no plotext/matplotlib) like the other text
  surfaces. three modes share one renderer in report.py: default latest-sample status
  table (state/rtt/loss/cpu/temp), --ranked incident ranking, and --heatmap (node x hour
  loss/rtt sparkline grid). reads the hub DB by default or the hub's read-only /api over
  http (--hub-url) so it works from any terminal; live repaint reuses the smoke live loop
  (--refresh, --bell on any down/stale). no --node needed - it shows the whole fleet.

- smoke footprint: read-only, pure-stdlib collector footprint report for node DBs (or
  --node on a hub DB). shows rows in the selected window, rows/day, SQLite bytes/day,
  and a shipper wire estimate using the same compact JSON+gzip shape as POST /ingest;
  --ship-rtts includes raw ping_rtts in the estimate.

- self panel (S5): the host collector records its own rss/cpu each cycle (proc_samples row
  named smokemon, via resource + /proc/self/statm); a new self panel graphs smokemon's own
  footprint over time to back the low-rss claim.

- synthetic transactions smokemon/probes/synthetic.py (X6): opt-in (SMOKEMON_SYNTHETIC=1)
  scripted checks beyond single-shot probes - captive-portal / interception detection
  (expects a 204 no-content) and a dns-over-https resolution check. new additive
  synthetic_samples table; runs on the slow tier; smokemon-synthetic entry point. pure urllib.

- lightweight external scrapes smokemon/probes/ext.py: opt-in SMOKEMON_EXT_HTTP endpoints
  for local app health/json/openmetrics signals. bounded by interval, timeout, response
  bytes, and metric caps; always stores up + latency_ms and only parses small JSON numeric
  fields or explicit OpenMetrics allowlists. no Docker logs, journal tails, or continuous
  log scanning on edge. new additive ext_metrics/ext_events tables; status/digest/incidents
  can surface external check state and scrape-failure events.

- native edge replacements for heavier Jetson monitors: host.py now reads Jetson GPU
  util/frequency from sysfs/devfreq (no tegrastats/nvidia-smi), and probes/redisq.py adds
  opt-in Redis stream health via tiny stdlib RESP socket reads (PING, INFO memory, XLEN,
  optional XPENDING). new additive gpu_samples/redis_samples tables; status/digest can
  surface GPU and Redis queue state.

- sonification (X2): --bell on smoke live/kiosk rings the terminal bell once on each
  transition into an unhealthy state (kiosk audible alerting).

- kiosk titles: kiosk keeps a minimal panel title (the label, with the live-stats suffix
  stripped) so each graph stays identifiable on a wall display; previously kiosk dropped
  panel titles entirely.

- bufferbloat grade (QW1): probes/iperf.py now parses the per-stream tcp rtt iperf3 reports
  (end.streams[].sender.mean_rtt) and stores it in the new additive
  iperf_samples.rtt_under_load_ms column. the renderers compare it against the idle ping
  baseline (largest per-target median) and annotate the iperf panel with a dslreports-style
  A+..F grade and the added latency under load.

- http layer-blame (QW2): the http panel names the dominant latency layer (dns / tcp connect
  / tls / server wait) by decomposing curl's cumulative phase timestamps already stored in
  http_samples. no schema change.

- death clocks (QW4): render-side linear extrapolation of disk_samples.used_pct (disk-full
  countdown), disk_health.wear_pct (sd/emmc wear countdown), and cpu-temp headroom to the
  throttle threshold (SMOKEMON_THROTTLE_TEMP, default 80C). surfaced as compact ~14d / ~3y /
  25C to throttle annotations on the disk and host panel titles.

- macos implementations for thermal / power / tcp panels (previously linux-only):

  - thermal: pmset -g therm -> cpu_speed_limit_pct pseudo-zone (100 = no thermal throttling,
    less means the kernel is capping clock speed for heat).

  - power: ioreg -rc AppleSmartBattery -> single battery rail with watts / volts / amps.
    empty on desktops (no battery).

  - tcp: netstat -s -p tcp and -p udp parsed for retransmits, rsts, rexmit drops, udp bad
    checksums, udp no-socket drops. conntrack remains none (pf state count needs root).

  - host panel also gets swap_used_pct (sysctl vm.swapusage) and cache_mb (vm_stat
    file-backed pages) on macos.

- linux-only by design: psi (no equivalent on macos without sudo powermetrics) and freq
  (apple silicon does not expose per-core clock speed without sudo).

changed:

- ship traffic trimmed (node net-footprint): raw per-ping rtts no longer ship to the hub by
  default - set SMOKEMON_SHIP_RTTS=1 to re-enable. the hub renders percentile bands from the
  pre-aggregated rtt_min/p25/median/p75/max in ping_runs and never reads raw ping_rtts for
  fresh rows, so this is ~85% fewer shipped rows for zero hub-side change; raw rtts stay in
  the node DB at full fidelity for local views. it also removes a latent catch-up spike
  (ping_rtts was exempt from the SHIP_BATCH cap, so a backlog could ship ~20x batch at once).

- /ingest bodies are gzipped (Content-Encoding: gzip; ~5-10x on numeric row-json, gzip level
  3 for sub-ms cpu on pi-class nodes). the hub decompresses by header and still accepts plain
  bodies, so node and hub upgrade together but neither rejects an un-gzipped payload.

fixed:

- concurrent-upgrade crash: ensure_body_columns and ensure_node_column now guard each ALTER
  TABLE ADD COLUMN against duplicate column name. on an in-place upgrade the two collector
  daemons (plus iperf) race the migration; the loser would hit that error, which is
  SQLITE_ERROR not SQLITE_BUSY, so busy_timeout does not retry it and the daemon would crash
  before its scheduler started.

- macos tcp metrics: netstat -s parsing is now plural-tolerant (data packets/bytes,
  connections dropped), so retrans_segs/estab_resets populate for real counts instead of
  reading null for anything above zero.

- jetson power: guarded the os.listdir of the INA3221 sysfs directory so a device
  unbind/suspend mid-cycle no longer raises and skips that cycle's host inserts.

- smoke live/kiosk flicker: the refresh loop repaints the frame in place - absolute per-row
  cursor addressing (ESC[row;1H), line-wrap disabled, and each line clipped to stay one
  row/column inside the terminal - instead of clearing the screen each tick. kills both the
  per-refresh blink and the ghosted/duplicated rows that showed when full-width plot lines
  wrapped onto an extra row and scrolled.

- hub dashboard plot view: the plotext braille markers come from a fallback font wider than
  the mono cell, so every data row drifted out of line with the ascii axes and the curve
  looked jagged. the dashboard now measures the braille-vs-mono cell delta at render time and
  pulls each braille run back to one cell with letter-spacing, so the plot graphs line up
  again. the terminal smoke tui was always correct (each line is exactly N columns) - this was
  a browser-font issue only.

- png dark/web theme gridlines: ax.grid(False, alpha=0.25) re-enabled the grid it meant to
  drop - matplotlib treats grid(False, <line-prop>) as "enable anyway", so the dashboard's
  dark graphs carried a grid the code intended to omit. the dark/web theme now renders
  gridless; the light desktop theme keeps its grid.

internal:

- hoisted the pi vcgencmd throttle-bit labels and decode into a single query.pi_bits_seen()
  helper, shared by both renderers (the png and tui copies had drifted apart).
```

```
== 0.11.0 - 2026-05-28  rich host metrics + grid layout ==

added:

- psi metrics: psi_cpu, psi_mem, psi_io (10-second rolling averages from /proc/pressure/*)
  in host_samples. early-warning signal for latency before resources hit 100%.

- memory pressure: swap_used_pct, cache_mb, oom_kill_count in host_samples.

- cpu frequency + throttle: cpu_freq_mhz, cpu_throttle_count, pi_throttle_bits in
  host_samples. detects silent perf regressions (100% busy at 600 MHz looks the same as at
  1500 MHz). the pi bit field (vcgencmd get_throttled) is sampled on a 5-minute slow tier.

- thermal zones table thermal_zones (ts, zone, temp_c): every zone sampled individually
  (jetson has ~10). the legacy temp_c column in host_samples still carries the max-of-zones
  for back-compat.

- power rails table power_samples (ts, rail, watts, volts, amps): jetson INA3221 i2c
  readings (/sys/bus/i2c/drivers/ina3221x/*).

- tcp/udp/conntrack table tcp_samples: kernel counters from /proc/net/snmp + conntrack fill
  from /proc/sys/net/netfilter/nf_conntrack_*.

- disk health table disk_health: sd/emmc wear-level (/sys/block/mmcblk*/device/life_time) on
  a 60-minute very-slow tier.

- wifi extras: bssid, retry_count, discard_count, beacon_loss in wifi_samples. roams across
  bssids are summarised in the render.

- inode usage: inode_used_pct in disk_samples.

- grid layout for plots: png and tui auto-arrange panels in a 2-column grid when there are
  >=3 panels and the canvas is wide enough. --cols N forces a specific count.

- five new panels: thermal, power, tcp, psi, freq. all optional and selected via --panels.

- migration: ensure_body_columns() ALTERs in any missing body columns on existing tables, so
  upgrades from older dbs are transparent (old rows get null for new columns).

changed:

- ping percentiles pre-aggregated: rtt_p25 and rtt_p75 are computed at insert time.
  load_ping_smoke reads them straight from ping_runs instead of scanning ping_rtts. old rows
  fall back to a join-based percentile calculation against a temp-id table (no in-list
  variable limit).

- hub ingest uses executemany for all non-ping tables (per-row execute is kept only for
  ping_runs where lastrowid is needed for the run_map). ~30-40% faster ingest on pi-class
  hardware.

- load_net uses sql LAG() for in-database delta computation when sqlite >= 3.25; falls back
  to the python loop otherwise.

- host._procs_linux uses os.scandir("/proc") instead of os.listdir.

- probes/http.py caches the curl path at import (was resolved on every probe).
```

```
== 0.10.0  package refactor ==

changed:

- package refactor: flat scripts collapsed into a smokemon/ package - config (env/node/
  paths), core (log/connect/signals/run_scheduler), schema (single-source ddl, generic
  insert), adapters/{darwin,linux}, probes/{ping,net,http,mtr,wifi,iperf,host}, collect (one
  daemon, group fast|slow|all), ship, hub, query (shared loaders + --node), render/{tui,png},
  cli (smoke subcommands).

- 3 collector daemons collapsed to 2 (fast=ping/net; slow=http/mtr/wifi/host). live.sh/
  daily_graph.sh replaced by smoke live/kiosk/daily.

- deduplication: schema, daemon loop, plot loaders, and a duplicate wifi_probe all
  consolidated. net caches the tailscale interface for 5 minutes. hub uses
  ThreadingHTTPServer + write lock.

- entry points: python -m smokemon.* (PYTHONPATH=repo, no install needed).
```

```
== 0.9.0  cross-platform + central aggregation ==

added:

- cross-platform adapters (platform.system() dispatch): linux paths use /proc/net/dev,
  tailscale0 (or 100.64.0.0/10 scan via ip), iw dev + /proc/net/wireless. macos paths
  unchanged. cli tool paths resolved via env -> shutil.which.

- node dimension: every table gains a node column (defaults to hostname, override via
  SMOKEMON_NODE). additive migration (alter + backfill) so node and hub dbs share one schema
  -> one plotter codebase.

- host health collector @30s. linux: cpu (/proc/stat), load, mem (/proc/meminfo), temp
  (/sys/class/thermal, incl. jetson), disk used (statvfs) + io (/proc/diskstats), top-n procs
  (/proc/<pid>/stat). macos subset. new tables: host_samples, disk_samples, proc_samples.

- central aggregation (push): shipper drains new rows per table (delta by id, cursor in
  ship_state) and posts to the hub. hub writes smokemon-hub.db with node + src_id,
  UNIQUE(node, src_id) + INSERT OR IGNORE in one transaction = idempotent. ping_rtts remapped
  to hub run ids, inserted only for newly-inserted runs. shared-secret header X-Smokemon-Key.

- plotters: --node filter (required on hub db). host + disk panels. matplotlib stays hub-only
  so nodes need only python3 stdlib + plotext (tui).

- linux deploy: install.sh (apt deps, setcap cap_net_raw on fping/mtr to skip sudo,
  /etc/smokemon.env, systemd units). macos launchd plists.
```

```
== 0.8.0  span-scaled png ==

changed:

- png width scales with time span (~2 inches per hour, clamp 16-80"). every 10s sample stays
  horizontally distinguishable. dpi lowered 130 -> 96 (granularity from width, not pixel
  density). 24h = ~4608xN px (~1 MB). new flags: --width, --dpi.
```

```
== 0.7.0  readable axes ==

changed:

- x ticks formatted %H:%M (never seconds), y integer ticks. http labels strip tld
  (cloudflare.com -> cloudflare). loss marker switched from X to braille dot. http lines use
  a fixed non-red palette (cyan/green/magenta) so they cannot be confused with red loss.
  kiosk keeps a subtle gray frame, titles off.
```

```
== 0.6.0  kiosk mode ==

added:

- kiosk mode (term_plot --kiosk, smokekiosk): no legend/ticks/labels/title/header.
```

```
== 0.5.0  active probes ==

added:

- active probes @60s: http timing via curl -sI -w (dns/connect/tls/ttfb/total), mtr --json
  -c10 for per-hop loss/avg/best/worst/stddev, wifi rssi/noise/tx/channel via
  system_profiler. mtr requires passwordless sudo (or setcap cap_net_raw on linux).

- iperf3 probe @900s: -J (up) + -R (down). consumes real bandwidth; requires iperf3 -s on
  the server.

- new tables: http_samples, mtr_hops, wifi_samples, iperf_samples. 6 panel types.
```

```
== 0.4.0  live window units ==

added:

- live window units (Nh/Nm/bare-number minutes): smokelive 24h 30.
```

```
== 0.3.0  tailscale target ==

added:

- third ping target reachable over the tailscale interface. tailscale interface
  auto-detected as the one with an address in 100.64.0.0/10; label tailscale (survives utun
  renumbering across reboots).

fixed:

- netstat -ibn rows for mac-less interfaces (utun) have 10 fields, not 11. the old code
  indexed columns wrong and silently skipped them; now we read the last 7 columns. utun was
  never captured before this.
```

```
== 0.2.0  tui + scheduling ==

added:

- tui: plotext braille plot (replaced a chafa inline-image poc).

- live + scheduled: live.sh, daily_graph.sh + launchd StartCalendarInterval at 23:55 ->
  graphs/daily/. zsh helpers smoke/smokelive/smokepng.
```

```
== 0.1.0  core collector ==

added:

- core collector @10s: fping -C20 -p50 (latency/loss + every individual rtt) + netstat -ibn
  (cumulative bytes -> mbit/s via delta/dt). default targets 1.1.1.1, 192.168.0.1.

- sqlite wal: ping_runs, ping_rtts, net_samples. no pruning (~5-6 gb/year; long-term plan is
  rollup/compression, never deletion of raw data).

- png renderer: matplotlib smoke-style plot (fill_between p0-p100 + p25-p75 + median + loss
  scatter).
```
