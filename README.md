# smokemon

[![CI](https://github.com/oovets/smokemon/actions/workflows/ci.yml/badge.svg)](https://github.com/oovets/smokemon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux-lightgrey.svg)](INSTALL.md)
[![Changelog](https://img.shields.io/badge/changelog-keep%20a%20changelog-orange.svg)](CHANGELOG.md)

smokemon is a passive + active network and host monitor for macos and linux, local and central. it collects ping latency/loss (fping), per-interface bandwidth (netstat / `/proc/net/dev`), HTTP timing (curl), mtr per-hop, WiFi signal (incl. retries + BSSID roams on linux), iperf3 throughput, host health (CPU/mem/temp/disk/processes), kernel pressure (PSI), TCP/UDP/conntrack counters, per-zone thermals, per-rail power on jetson (INA3221), CPU frequency + throttle counters, and SD-card wear-level - all into SQLite (WAL), viewed as a plotext braille TUI or a matplotlib PNG in a configurable grid. daemons run via launchd (macOS) or systemd (Linux); nodes push row deltas to a central hub for aggregated, per-node views. footprint is about 30 MB RSS per node (two collector daemons) and well under 1% of one core on average; the hub adds ~20 MB.

```
view:    smoke · smoke live 24h · smoke kiosk 24h · smoke png   (or python -m smokemon.cli …)
run:     python -m smokemon.collect {fast|slow}   (launchd/systemd do this; see deploy/)
install: macOS  cp deploy/launchd/*.plist ~/Library/LaunchAgents/ && bootstrap each
         Linux  curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
                  | sudo bash -s -- --node NAME [--hub-url URL --secret S]
full reference -> INSTALL.md
```

full version history -> [CHANGELOG.md](CHANGELOG.md)
roadmap / ideas -> [PLAN.md](PLAN.md)

```
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

earlier versions (v0.1 - v0.9) -> [CHANGELOG.md](CHANGELOG.md)
```

```
smoke [tui]                 static TUI; 13 panel types: ping,net,http,mtr,wifi,iperf,
                            host,disk,thermal,power,tcp,psi,freq|all   --cols N|0(auto)
                            psi+freq are Linux-only; thermal/power/tcp also work on macOS
                            (cpu_speed_limit, battery rail, netstat -s parsing)
smoke live 24h | smoke kiosk 24h [--refresh N]      live / clean wall display
smoke png [--width N --dpi N --cols N] | smoke daily   PNG -> Preview / dated 24h PNG
common: --minutes N|--hours N|--since|--until --targets --panels --node (req. on hub DB)

daemons: python -m smokemon.collect {fast|slow} | .probes.iperf | .ship | .hub  (PYTHONPATH=repo)

multi-node: nodes run collect + iperf + ship (push delta -> hub); hub runs
            python -m smokemon.hub (-> smokemon-hub.db). plot on hub with --node NAME.

deploy: macOS deploy/launchd/*.plist (collect-fast/slow, iperf, daily, shipper, hub);
        Linux sudo ./install.sh --node NAME --hub-url URL --secret S
        (hub: --hub --secret S). secret must match node<->hub.

deps:   node: fping,mtr,iperf3,iw + python3 stdlib + plotext(TUI); hub: +matplotlib/numpy(PNG).
```
