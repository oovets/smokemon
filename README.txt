smokemon - passive+active network + host monitor (macOS + Linux/RPi/Jetson; local + central).
fping/curl/mtr/iperf3 + /proc,/sys (Linux) or netstat/system_profiler (macOS) -> SQLite(WAL)
-> plotext TUI / matplotlib PNG. launchd (macOS) or systemd (Linux) daemons; nodes push row
deltas to a central hub (app01). ~25 MB RSS, <1% of one core avg.

CHANGELOG (newest first, all 2026-05-28):

== v0.9  cross-platform + central aggregation ==
- platform_adapters.py: OS dispatch (platform.system()) for net counters / Tailscale
  iface / WiFi so collectors are OS-agnostic. Linux: /proc/net/dev (no fragile netstat
  parsing), tailscale0 (or 100.64/10 scan via `ip`), `iw dev`+/proc/net/wireless. macOS
  paths unchanged. CLI paths via env -> shutil.which fallback (no hardcoded /opt/homebrew).
- node dimension: every table gains a `node` column (default socket.gethostname(),
  override SMOKEMON_NODE). Additive migration (ensure_node_column: ALTER + backfill) so
  node DB and hub DB share ONE schema -> one plotter codebase.
- collector_host.py @30s: full host health. Linux: cpu (/proc/stat), load, mem
  (/proc/meminfo), temp (/sys/class/thermal incl Jetson), disk used (statvfs) + disk IO
  (/proc/diskstats), top-N procs by cpu (/proc/<pid>/stat). macOS: subset (load/mem/disk/
  procs via ps; temp+IO skipped). new tables: host_samples, disk_samples, proc_samples.
- central aggregation (push): shipper.py drains new rows per table (delta by id, cursor
  in ship_state) and POSTs to hub_ingest.py. hub_ingest (stdlib http.server) writes
  smokemon-hub.db with node+src_id, UNIQUE(node,src_id) + INSERT OR IGNORE in one txn =
  idempotent. ping_rtts translated to hub run ids, inserted only for newly-inserted runs
  (no dupes on retry). shared-secret auth header X-Smokemon-Key.
- plotters: --node filter (required on hub DB); new host + disk panels. matplotlib stays
  hub-only -> nodes need only python3 stdlib + plotext(TUI).
- deploy: systemd/ unit+timer templates + scripts/install_linux.sh (apt deps, setcap
  cap_net_raw on fping/mtr to skip sudo, /etc/smokemon.env, enable units). launchd/ plists
  for new host+shipper services on macOS. live.sh/daily_graph.sh python path via SMOKEMON_PY.

== v0.8  PNG granularity ==
- plot.py: figure width prop. to time span (~2 in/h, clamp 16-80") -> every 10s
  sample stays horizontally distinguishable. dpi 130->96 (granularity from width,
  not pixel density). 24h ~= 4608xN px / ~1 MB. new flags: --width --dpi.

== v0.7  axes + cosmetics (TUI) ==
- X ticks %H:%M (never seconds); Y integer ticks (plotext: own ticks via
  step=max(1,range/5); PNG: MaxNLocator(integer)).
- HTTP labels strip TLD (cloudflare.com -> cloudflare).
- loss marker X -> "dot" (.); HTTP lines use a fixed non-red palette (cyan/green/
  magenta) -> no confusion with red loss.
- kiosk: keep frame but ticks_color=240 (subtle dark gray); titles off.

== v0.6  kiosk mode ==
- term_plot --kiosk + smokekiosk: no legend/ticks/labels/title/header.
  helpers L()/_ylabel()/_title() no-op in kiosk; xfrequency(0)/yfrequency(0).

== v0.5  active probes (sudo + bandwidth) ==
- collector_probes.py @60s (launchd KeepAlive):
  * HTTP: curl -sI -w -> ms breakdown DNS/connect/TLS/TTFB/total (HEAD = no body).
  * mtr : sudo -n mtr -n --json -c10 -i0.2 -> per-hop loss/avg/best/wrst/stddev.
          REQUIRES passwordless sudo (sudo -n) for the user agent.
  * WiFi: system_profiler SPAirPortDataType -> RSSI/noise/tx/channel (no sudo;
          ~3s wall / 0.23s CPU = heaviest probe).
- iperf_probe.py @900s (StartInterval): iperf3 -J (up) + -R (down) to 100.87.219.2
  (app01; REQUIRES iperf3 -s there). consumes real bandwidth.
- new tables: http_samples, mtr_hops, wifi_samples, iperf_samples.
- plotters: 6 panel types + --panels {ping,net,http,mtr,wifi,iperf|all}.

== v0.4  smokelive window units ==
- live.sh accepts Nh/Nm/bare-number(min). ex: smokelive 24h 30 (live trend view).

== v0.3  Tailscale + interface fix ==
- 3rd target 100.127.203.7 (vpn, via utun).
- TS iface auto-detect: iface with an addr in 100.64.0.0/10 -> label "tailscale"
  (dynamic, survives utun renumbering across reboots).
- BUG FIX: netstat -ibn rows for MAC-less ifaces (utun…) have 10 fields not 11 ->
  index the last 7 columns. (utun was NEVER captured before this.)
- TARGET_LABELS: 1.1.1.1=internet, 192.168.0.1=gw, 100.127.203.7=vpn.

== v0.2  TUI + schedules ==
- term_plot.py: plotext braille plot (replaced a chafa inline-image PoC; wanted a
  text TUI, not pixels via the WezTerm graphics protocol).
- live.sh (smokelive); daily_graph.sh + launchd StartCalendarInterval 23:55 ->
  graphs/daily/. zsh functions: smoke/smokelive/smokepng.

== v0.1  core ==
- collector.py @10s (launchd RunAtLoad+KeepAlive): fping -C20 -p50 (lat/loss + every
  individual RTT) + netstat -ibn (cumulative bytes -> Mbit/s via delta/dt).
  targets 1.1.1.1,192.168.0.1. Tailscale iface labeled "tailscale".
- SQLite WAL: ping_runs, ping_rtts, net_samples. NO pruning (long-term data,
  ~5-6 GB/yr; if needed: rollup/compression, not deletion of raw data).
- plot.py: matplotlib smoke (fill_between p0-p100 + p25-p75 + median + loss scatter).

== QUICKREF ==
  smoke [--panels …|--kiosk] [--minutes N|--hours N] [--node NAME]   TUI (panels incl host,disk)
  smokelive 24h [sec] | smokekiosk 24h [sec]                         live / wall display
  smokepng [--width " --dpi N --panels … --node NAME]                PNG (hub: --node required)
  panels: ping,net,http,mtr,wifi,iperf,host,disk  (host=cpu/mem/temp, disk=used% per mount)
  multi-node: nodes run collector/probes/host + shipper.py (push delta -> hub); hub runs
          hub_ingest.py (-> smokemon-hub.db). On the hub, plot with --node NAME per node.
  install Linux node: sudo scripts/install_linux.sh --node NAME --hub-url URL --secret S
  install Linux hub:  sudo scripts/install_linux.sh --hub --secret S   (must match node secret)
  config: env vars (SMOKEMON_*): macOS launchd plists (~/Library/LaunchAgents/com.stefan.
          smokemon{,-probes,-host,-iperf,-shipper,-daily}.plist); Linux /etc/smokemon.env.
  jobs:   macOS: launchctl [bootout|bootstrap] gui/$(id -u) <plist>;  Linux: systemctl.
  deps:   node: fping,mtr,iperf3,iw (apt) + python3 stdlib + plotext(TUI); macOS via brew +
          curl/netstat/ifconfig/system_profiler. hub: python3 + matplotlib/numpy (PNG).
