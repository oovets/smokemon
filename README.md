# smokemon

smokemon is a passive + active network and host monitor for macos and linux, local and central. it collects ping latency/loss (fping), per-interface bandwidth (netstat / `/proc/net/dev`), HTTP timing (curl), mtr per-hop, WiFi signal, iperf3 throughput and host health (CPU/mem/temp/disk/processes) into SQLite (WAL), viewed as a plotext braille TUI or a matplotlib PNG. daemons run via launchd (macOS) or systemd (Linux); nodes push row deltas to a central hub for aggregated, per-node views. footprint is about 30 MB RSS per node (two collector daemons) and well under 1% of one core on average; the hub adds ~20 MB.

## How to

```
view:    smoke · smoke live 24h · smoke kiosk 24h · smoke png   (or python -m smokemon.cli …)
run:     python -m smokemon.collect {fast|slow}   (launchd/systemd do this; see deploy/)
install: macOS  cp deploy/launchd/*.plist ~/Library/LaunchAgents/ && bootstrap each
         Linux  curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
                  | sudo bash -s -- --node NAME [--hub-url URL --secret S]
full reference -> INSTALL.txt
```

## Changelog (newest first, all 2026-05-28)

```
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

== v0.9  cross-platform + central aggregation ==
- platform dispatch (platform.system()) for net counters / Tailscale iface / WiFi so
  collectors are OS-agnostic. Linux: /proc/net/dev, tailscale0 (or 100.64/10 scan via
  `ip`), `iw dev`+/proc/net/wireless. macOS paths unchanged. CLI paths via env -> which.
- node dimension: every table gains a `node` column (default hostname, override
  SMOKEMON_NODE). Additive migration (ALTER + backfill) so node DB and hub DB share ONE
  schema -> one plotter codebase.
- host health @30s. Linux: cpu (/proc/stat), load, mem (/proc/meminfo), temp
  (/sys/class/thermal incl Jetson), disk used (statvfs) + IO (/proc/diskstats), top-N
  procs (/proc/<pid>/stat). macOS: subset. new tables: host_samples, disk_samples,
  proc_samples.
- central aggregation (push): ship drains new rows per table (delta by id, cursor in
  ship_state) and POSTs to the hub. hub writes smokemon-hub.db with node+src_id,
  UNIQUE(node,src_id) + INSERT OR IGNORE in one txn = idempotent. ping_rtts remapped to
  hub run ids, inserted only for newly-inserted runs. shared-secret header X-Smokemon-Key.
- plotters: --node filter (required on hub DB); host + disk panels. matplotlib stays
  hub-only -> nodes need only python3 stdlib + plotext (TUI).
- deploy: systemd units + install_linux.sh (apt deps, setcap cap_net_raw on fping/mtr to
  skip sudo, /etc/smokemon.env, enable units); launchd plists for macOS.

== v0.8  PNG granularity ==
- figure width prop. to time span (~2 in/h, clamp 16-80") -> every 10s sample stays
  horizontally distinguishable. dpi 130->96 (granularity from width, not pixel density).
  24h ~= 4608xN px / ~1 MB. flags: --width --dpi.

== v0.7  axes + cosmetics (TUI) ==
- X ticks %H:%M (never seconds); Y integer ticks. HTTP labels strip TLD
  (cloudflare.com -> cloudflare). loss marker uses braille dots (red), not X. HTTP lines
  use a fixed non-red palette (cyan/green/magenta). kiosk keeps a subtle gray frame,
  titles off.

== v0.6  kiosk mode ==
- term_plot --kiosk + smokekiosk: no legend/ticks/labels/title/header.

== v0.5  active probes (sudo + bandwidth) ==
- probes @60s: HTTP (curl -sI -w -> DNS/connect/TLS/TTFB/total, HEAD = no body); mtr
  (sudo -n mtr -n --json -c10 -i0.2 -> per-hop loss/avg/best/wrst/stddev, needs
  passwordless sudo); WiFi (system_profiler -> RSSI/noise/tx/channel).
- iperf @900s: iperf3 -J (up) + -R (down); consumes real bandwidth; needs iperf3 -s.
- new tables: http_samples, mtr_hops, wifi_samples, iperf_samples. 6 panel types.

== v0.4  smokelive window units ==
- live accepts Nh/Nm/bare-number(min). ex: smokelive 24h 30 (live trend view).

== v0.3  Tailscale + interface fix ==
- a vpn target reached over the Tailscale interface. TS iface auto-detect: addr in
  100.64.0.0/10 -> label "tailscale" (survives utun renumbering). BUG FIX: netstat -ibn
  rows for MAC-less ifaces (utun) have 10 fields not 11 -> index the last 7 columns (utun
  was NEVER captured before). labels: 1.1.1.1=internet, 192.168.0.1=gw, plus a vpn label.

== v0.2  TUI + schedules ==
- plotext braille plot (replaced a chafa inline-image PoC; wanted a text TUI). live
  (smokelive); daily PNG via launchd 23:55. zsh functions smoke/smokelive/smokepng.

== v0.1  core ==
- collector @10s: fping -C20 -p50 (lat/loss + every individual RTT) + netstat -ibn
  (cumulative bytes -> Mbit/s via delta/dt). targets 1.1.1.1,192.168.0.1.
- SQLite WAL: ping_runs, ping_rtts, net_samples. NO pruning (~5-6 GB/yr; if needed:
  rollup/compression, not deletion of raw data). matplotlib smoke (fill_between
  p0-p100 + p25-p75 + median + loss scatter).
```

## Quick reference

```
smoke [tui]                 static TUI (panels: ping,net,http,mtr,wifi,iperf,host,disk|all)
smoke live 24h | smoke kiosk 24h [--refresh N]      live / clean wall display
smoke png [--width " --dpi N] | smoke daily         PNG -> Preview / dated 24h PNG
common: --minutes N|--hours N|--since|--until --targets --panels --node (req. on hub DB)

daemons: python -m smokemon.collect {fast|slow} | .probes.iperf | .ship | .hub  (PYTHONPATH=repo)

multi-node: nodes run collect + iperf + ship (push delta -> hub); hub runs
            python -m smokemon.hub (-> smokemon-hub.db). plot on hub with --node NAME.

deploy: macOS deploy/launchd/*.plist (collect-fast/slow, iperf, daily, shipper, hub);
        Linux sudo deploy/install_linux.sh --node NAME --hub-url URL --secret S
        (hub: --hub --secret S). secret must match node<->hub.

deps:   node: fping,mtr,iperf3,iw + python3 stdlib + plotext(TUI); hub: +matplotlib/numpy(PNG).
```
