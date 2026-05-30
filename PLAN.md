# smokemon - ideas roadmap

an idea catalog, not a commitment. creative directions worth building, ordered by leverage.
entries become real work only when promoted into the changelog and code.

status: each entry below is tagged [done] / [deferred]. [done] shipped in 0.12.0 (see
CHANGELOG) - the analysis engine (`smokemon/analyze.py`), text surfaces (`smokemon/report.py`:
`smoke status|incidents|digest`, `smoke replay`), alerting (`smokemon/notify.py`), the hub
`/metrics` + `/api/*` + live dashboard (`GET /`) and terminal `smoke fleet`
(`smokemon/hubapi.py`), the `self` panel and the opt-in `smokemon/probes/synthetic.py`.
[deferred] needs hardware / a gpu / a multi-day redesign that can't be built + verified
blind: X1 (gpio/led), X3 (jetson on-device ml), X4 (hubless mesh gossip).

the thread running through all of it: smokemon's untapped edge is synchronized
multi-signal data on one timeline per node. smokeping sees only the network, netdata only the
host, wifi tools only the radio - smokemon already stores ping loss/rtt-spread, bandwidth,
http layer breakdown, per-hop mtr, wifi rssi/roams, iperf throughput, cpu/mem/temp/psi/freq,
per-zone thermals, per-rail power, tcp/conntrack counters, disk/sd-wear and top processes all
under the same `ts` and `node`. so it can answer what no single-domain tool can: what else was
happening at the exact moment things went bad. almost every idea below exploits data already
collected in new combinations, not new collection.

full version history -> [CHANGELOG.md](CHANGELOG.md)
project conventions -> [CONTRIBUTING.md](CONTRIBUTING.md)

```
each entry -> what · data (existing tables/fields) · code (where it lives) · surface
              (tui/png/cli/hub/file) · effort S(hours)/M(day-two)/L(multi-day) · fit
              (stdlib-only on node? rss impact? additive schema? opt-in deps?)
```

```
== tier 1 - quick wins (data already exists, low effort) ==

QW1 [done] bufferbloat grade (A-F). iperf3 -J already returns streams[].rtt_min/mean/max which
    probes/iperf.py does not parse yet. combine idle ping vs ping-under-load -> dslreports-
    style grade. surface: iperf panel annotation + digest. effort S. additive column
    rtt_under_load_ms on iperf_samples; iperf is already the 15-min tier so no loop cost.

QW2 [done] http layer-blame. http_samples already splits dns/connect/tls/ttfb. name the dominant
    contributor ("slow = dns resolver, not your link"). code: loader in query.py, render in
    render/png.py + render/tui.py. surface: stacked http panel + culprit label. effort S.
    no schema change.

QW3 [done] sparkline status line. one glanceable row, unicode sparklines:
    internet _.-^-. 4ms · wifi ^^- -52dBm · cpu .._ 45C · healthy. new `smoke status`
    subcommand in cli.py, reuses existing loaders. drops into a cursor statusline / tmux /
    macos menubar. effort S. pure stdlib.

QW4 [done] death clocks. linear-extrapolate disk_samples.used_pct growth, disk_health.wear_pct
    trend, temp-to-throttle headroom -> countdowns ("disk full ~14d", "sd ~3y left", "temp
    ~6C from throttle"). code: query.py + a status/digest line. effort S-M. no schema change.
```

```
== tier 2 - flagship (unique to the synchronized data) ==

F1  [done] blame engine (multi-signal incident correlation). on a latency/loss spike, correlate
    against cpu, temp, wifi rssi, bssid roam, bandwidth, bufferbloat and newly-appeared
    procs -> human-readable cause list ("14:32-14:35 latency +400% - correlates with cpu
    98% + new process backup + temp 71C"). method: pearson/spearman + lag alignment, pure
    stdlib, hub-side at render/report time. code: new read-only smokemon/analyze.py +
    `smoke incidents`. effort M. zero node impact.

F2  [done] event/incident detection. rules over the multi-signal data: link down (loss=100% N
    cycles), isp outage (gw ok + internet loss) vs upstream, dns-slow-but-tcp-fast,
    roam-correlated throughput dip. output: incidents table with start/end/duration/class.
    code: smokemon/analyze.py. surface: tui table + digest. effort M.

F3  [done] plain-english daily digest. narrative built on F1/F2: uptime %, blips + total duration,
    peak latency and what it coincided with, bufferbloat grade, roam count, thermals. code:
    `smoke digest`. surface: text file / stdout / optional push (S4). effort M.
```

```
== tier 3 - predictive / statistical (stdlib only) ==

P1  [done] time-of-day anomaly baseline. "abnormal for a tuesday 14:00" with no thresholds, no ml.
    baseline per hour-of-day/day-of-week from retained raw ping_rtts; flag via rolling
    median + MAD z-score. code: analyze.py. effort M. hub-side.

P2  [done] change-point / regime-shift detection. catch silent changes - isp dropped your speed
    tier, a new device saturates wifi, a route changed permanently ("bandwidth regime shift
    03:00 - median 940->230 Mbps"). cusum or rolling mean-shift. code: analyze.py. effort M.

P3  [done] path intelligence from mtr. detect route changes over time, attribute the bad hop (which
    hop adds loss/latency), compute a path-stability score from mtr_hops. surface: enhanced
    mtr panel + incidents. effort M.
```

```
== tier 4 - surfaces and interop ==

S1  [done] dvr scrubber. raw data is kept forever -> replay any historical window like a tape deck:
    `smoke replay 2026-05-20`, arrow keys to scrub. code: cli.py + render/tui.py. effort M.

S2  [done] prometheus / openmetrics endpoint. a /metrics route on the existing hub server (hub.py
    already runs http.server) exposing latest values -> plugs into grafana/alertmanager.
    effort S-M. stdlib, hub-side.

S3  [done] read-only json api + fleet view. /api for latest/aggregated data; fleet ranking and a
    node x hour heatmap when multiple nodes report. code: hub.py + query.py. effort M.

S4  [done] push / webhook alerting. fire ntfy/slack/webhook/email from F2 incidents. hub-side
    notifier using urllib. effort S-M. stdlib.

S5  [done] self-instrumentation panel. smokemon already shows up in proc_samples (it reads its own
    rss) -> graph its own footprint/cpu over time to prove the low-rss claim. new `self`
    panel. effort S.
```

```
== tier 5 - frontier / experimental (opt-in; may need extras or hardware) ==

X1  [deferred] gpio/led ambient health. a physical led on a pi that goes red on loss/incident. node-
    side, gated behind an opt-in extra (gpiozero/RPi.GPIO) so the stdlib-only core is
    untouched. effort S-M.

X2  [done] sonification / audible alerts. terminal-bell patterns or a tone whose pitch tracks
    health, for kiosk mode. code: render/tui.py / cli. effort S. stdlib.

X3  [deferred] on-device ml on jetson. a tiny anomaly autoencoder running only where there's a gpu.
    jetson-only, strictly opt-in deps; hub and pi stay stdlib. effort L.

X4  [deferred] hubless mesh gossip. nodes exchange row deltas peer-to-peer so a fleet works with no
    central hub; UNIQUE(node,src_id) + INSERT OR IGNORE already make merges idempotent.
    code: gossip variant of ship.py/hub.py. effort L. stdlib, big design.

X5  [done] bandwidth attribution. "what's hammering my network" - correlate net_samples spikes with
    proc_samples (optionally per-process net counters) to name the culprit process. code:
    analyze.py + optional probe. effort M.

X6  [done] synthetic transactions. scripted multi-step checks beyond single-shot probes - login
    flow, dns-over-https, captive-portal detection. new module under probes/. effort M.
```

```
== recommended build order ==

1. QW1 + QW2 + QW4   cheapest, data already present, immediate wow.
2. F1 + F2           the genuinely novel core, in a new read-only smokemon/analyze.py.
3. F3 + S4           digest + alerting on top of F2.
4. S2                prometheus endpoint - large ecosystem reach for little code.
5. P1-P3 / X*        predictive + frontier, by appetite.
```

```
== guardrails (must hold for any of the above) ==

- node-side code stays stdlib-only. analysis/ml/exporters live on the hub or behind opt-in
  extras, never imported by collectors/ship/probes/adapters.

- no rss regressions on the node. the project deliberately reverted the SQLite cache/mmap
  PRAGMAs for this reason; see the docstring in smokemon/core.py.

- schema changes are additive only, via ensure_body_columns in smokemon/schema.py (ALTER
  ADD, never DROP/RENAME). wire-format changes stay backward compatible.

- no fabricated data - everything derives from real collected metrics, never seed/mock/
  guessed values.

- new heavy work goes to the slow/vslow tiers or hub-side render time, never the 10s fast
  loop.
```
