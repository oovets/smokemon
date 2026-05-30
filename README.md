# smokemon

> full-stack network + host monitoring for the edge — every signal on one timeline, in stdlib python and ~30 mb of ram. no cloud, no dependencies, nothing to install but python.

smokemon watches network and the box it runs on — ping loss & latency spread, bandwidth, http breakdown, per-hop routes, wifi, throughput, cpu/mem/temp/psi/power — and lays it all on a single timeline, so you can see what else was happening the moment things went bad.

the core is pure-stdlib python: a raspberry pi or jetson runs it for ~30 mb of ram and well under 1% of one core — it graphs its own footprint to prove it. point many nodes at one hub and watch the whole fleet from a terminal or a browser.

[![CI](https://img.shields.io/github/actions/workflow/status/oovets/smokemon/ci.yml?branch=main&label=CI)](https://github.com/oovets/smokemon/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue.svg)](https://oovets.github.io/smokemon/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux-lightgrey.svg)](INSTALL.md)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Core deps: zero](https://img.shields.io/badge/core%20deps-zero%20%28stdlib%29-brightgreen.svg)](pyproject.toml)

**new here?** copy-paste install & use in [QUICKSTART.md](QUICKSTART.md). full reference below.

```
view:    smoke
         smoke live 24h
         smoke kiosk 24h
         smoke png (or python -m smokemon.cli …)

run:     python -m smokemon.collect {fast|slow}
         (launchd/systemd do this; see deploy/)

macOS    cp deploy/launchd/*.plist ~/Library/LaunchAgents/ && bootstrap each

Linux    curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
            | sudo bash -s -- --node NAME [--hub-url URL --secret S]
```

```
== analysis engine + dashboard + alerting ==

- smokemon/analyze.py (hub-side, read-only, stdlib): incident detection (isp-outage /
  link-down / packet-loss / latency-spike / dns-slow), multi-signal blame (what deviated
  during the window + new processes), time-of-day anomaly baseline, change-point detection,
  mtr path intelligence, bandwidth attribution.

- text surfaces (run on a node too): `smoke status` (sparkline health line), `smoke
  incidents` (incidents + blame), `smoke digest` (plain-english summary). `smoke replay`
  scrubs any past window. `--bell` rings on degraded health; `--notify` pushes incidents.

- hub now serves a live fleet dashboard at GET / , a prometheus /metrics endpoint, and
  read-only /api/{nodes,latest,fleet,heatmap,fleet-status}. push alerts via
  smokemon/notify.py (ntfy/slack/discord/webhook).

- node-side: a `self` panel graphs smokemon's own RSS/CPU; opt-in synthetic transactions
  (captive-portal + DoH) via probes/synthetic.py + the additive synthetic_samples table.

== v0.11  rich host metrics + grid layout ==

- new tables: thermal_zones (all sensors, not just max), power_samples (jetson INA3221
  per-rail watts), tcp_samples (retransmits / RSTs / udp errors / conntrack fill),
  disk_health (SD wear-level, hourly). host_samples adds PSI cpu/mem/io, swap/cache,
  oom_kill_count, cpu_freq_mhz, cpu_throttle_count, pi_throttle_bits. wifi_samples adds
  bssid + retry/discard/beacon counters; render shows roam count across BSSIDs.

- renderer: 5 new panels (thermal, power, tcp, psi, freq). 2-col grid by default
  (PNG when >=3 panels, TUI when terminal >=140 cols). --cols N to force.

- perf: ping_rtt percentiles (p25/p75) pre-aggregated at insert -> load_ping_smoke skips
  the ping_rtts scan for new rows. hub ingest uses executemany. load_net uses SQL LAG()
  (sqlite >=3.25). SQLite stays on WAL + synchronous=NORMAL only; cache/mmap PRAGMAs were
  tried and reverted to keep node RSS low (smokemon reports its own RSS, so they'd skew it).

== v0.10  package refactor ==

- flat scripts -> smokemon/ package: config (env/NODE/paths), core (log/connect/
  signals/run_scheduler), schema (single-source DDL -> node+hub + STD_TABLES + generic
  insert), adapters/{darwin,linux}, probes/{ping,net,http,mtr,wifi,iperf,host}, collect
  (one daemon, group fast|slow|all), ship, hub, query (shared loaders + --node),
  render/{tui,png}, cli (`smoke` subcommands).

- 3 collector daemons -> 2 (fast=ping/net; slow=http/mtr/wifi/host). live.sh/daily_graph.sh
  -> `smoke live|kiosk|daily`. dedup: schema, daemon loop, plot loaders, the duplicate
  wifi_probe (all gone). net caches the TS iface (5 min). hub: ThreadingHTTPServer + write
  lock. entrypoints: python -m smokemon.* (PYTHONPATH=repo, no install needed).
```

earlier versions (v0.1 - v0.9) → [CHANGELOG.md](CHANGELOG.md)

```
smoke [tui]                 static TUI; 14 panel types: ping,net,http,mtr,wifi,iperf,
                            host,disk,thermal,power,tcp,psi,freq,self|all  --cols N|0(auto)
                            psi+freq are Linux-only; thermal/power/tcp also work on macOS
                            (cpu_speed_limit, battery rail, netstat -s parsing)

smoke live 24h | smoke kiosk 24h [--refresh N] [--bell]   live / clean wall display

smoke replay [DATE|Nh] [--frame MIN] DVR scrubber (←/→ scrub, ↑/↓ step, q)

smoke fleet [live]         aggregated terminal view of every node reporting to the hub
                           (worst-first, colour-coded; the TUI twin of GET /). --ranked
                           for uptime/downtime over --hours; --heatmap [--metric loss|rtt]
                           for a node×hour sparkline grid; --hub-url URL reads the hub's
                           read-only /api over HTTP (no hub DB access needed); --bell.

smoke png [--width N --dpi N --cols N] | smoke daily   PNG -> Preview / dated 24h PNG

smoke status | smoke incidents | smoke digest [--notify]   text analysis (stdlib, node-ok)
common: --minutes N|--hours N|--since|--until --targets --panels --node (req. on hub DB)

analysis: smokemon/analyze.py (incident detection + multi-signal blame + anomaly/change-
          point/path/attribution stats, hub-side read-only). hub also serves a live fleet
          dashboard at GET / , plus GET /metrics (prometheus) and
          GET /api/{nodes,latest,fleet,heatmap,fleet-status}.

alerting: set SMOKEMON_NOTIFY_URL (ntfy/slack/discord/webhook) + `smoke digest --notify`
          or the smokemon-notify timer. synthetic checks: SMOKEMON_SYNTHETIC=1.

daemons: python -m smokemon.collect {fast|slow} | .probes.iperf | .probes.synthetic
         | .ship | .hub | .notify  (PYTHONPATH=repo)

multi-node: nodes run collect + iperf + ship (push delta -> hub); hub runs
            python -m smokemon.hub (-> smokemon-hub.db). plot a single node with
            --node NAME; see the whole fleet at once with `smoke fleet` (or GET /).
            repoint a node with `smoke hub NEW-HUB` (writes SMOKEMON_HUB_URL).

deploy: macOS deploy/launchd/*.plist (collect-fast/slow, iperf, daily, shipper, hub);
        Linux sudo ./install.sh --node NAME --hub-url URL --secret S
        (hub: --hub --secret S). secret must match node<->hub.

deps:   node: fping,mtr,iperf3,iw + python3 stdlib + plotext(TUI);
        hub: +matplotlib/numpy(PNG) + iperf3 (runs iperf3 -s as the nodes' bandwidth target).
```

```
== what the metrics mean (the non-obvious ones) ==

rtt spread        the p25-p75 / p0-p100 band around median ping, not a single number - a
                  wide band = jitter even when the average looks fine.
bufferbloat grade A+..F from idle ping vs ping-under-load (iperf). F = the link buffers
                  badly under load (calls/games stutter while something downloads).
psi               linux pressure-stall info (/proc/pressure): % of time tasks stalled on
                  cpu/mem/io. rises *before* utilisation hits 100% - an early warning.
conntrack fill    how full the kernel's connection-tracking table is. near 100% = new
                  connections get dropped (looks like packet loss, isn't the link).
death clocks      linear extrapolation to a limit: disk-full eta, sd/emmc wear-out eta, and
                  headroom (degC) before the cpu thermally throttles.
roam count        how many times wifi jumped between bssids (access points) in the window;
                  frequent roams correlate with throughput dips.
throttle bits     raspberry pi vcgencmd flags (under-voltage / freq-capped / throttled),
                  past and currently-active - the usual cause of silent pi slowdowns.
```

```
== what the metrics mean (the non-obvious ones) ==

rtt spread        the p25-p75 / p0-p100 band around median ping, not a single number - a
                  wide band = jitter even when the average looks fine.
bufferbloat grade A+..F from idle ping vs ping-under-load (iperf). F = the link buffers
                  badly under load (calls/games stutter while something downloads).
psi               linux pressure-stall info (/proc/pressure): % of time tasks stalled on
                  cpu/mem/io. rises *before* utilisation hits 100% - an early warning.
conntrack fill    how full the kernel's connection-tracking table is. near 100% = new
                  connections get dropped (looks like packet loss, isn't the link).
death clocks      linear extrapolation to a limit: disk-full eta, sd/emmc wear-out eta, and
                  headroom (degC) before the cpu thermally throttles.
roam count        how many times wifi jumped between bssids (access points) in the window;
                  frequent roams correlate with throughput dips.
throttle bits     raspberry pi vcgencmd flags (under-voltage / freq-capped / throttled),
                  past and currently-active - the usual cause of silent pi slowdowns.
```
