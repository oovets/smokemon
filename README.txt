smokemon - passive+active network monitor (macOS, local). fping/netstat/curl/mtr/
iperf3/system_profiler -> SQLite(WAL) -> plotext TUI / matplotlib PNG. launchd daemons.
~25 MB RSS, <1% of one core avg.

== TUI (smoke / smokelive; colors stripped here, normally green/orange/red) ==

  internet (1.1.1.1)   median now 4 ms · spread (gray) min–max · avg loss 2%
  ┌────────────────────────────────────────────────────────────────────┐
47┤ ⢕⢕ median  ⢰        ⢠  ⣄ ⢀ ⣀⡄⣶  ⢸    ⣄        •       •⡆⡀          │
38┤ •• loss    ⢸⣀     ⣠⣠⢸⢰ ⣿ ⢸ ⣿⡇⣿  ⣸⣀⡄ ⣀⣿    ⢀   ⡇⣴⣦⢸   ⣀•⡇⡇  ⡀ ⡆⡀  ⢀ │
29┤⣿⣠⣼⢰⡇ ⡇⢰⣇⢸⣿⢀⣾⣿ ⡄ ⣦ ⣿⣿•⣼⢸⣿⣦⢸ ⣿⡇⣿⡀⢰⣿⣿⡇⣠⣿⣿⢰⡄  ⢸   ⡇⣿⣿⢸ ⢰⡇⣿⢸⡇⡇⣴ ⡇⢸⣿⣷  ⢸⢰│
20┤⣿⣿⣿⣿⣷⣼⣷⡿⣿⣾⣿⣿•⣿⣼⣧⣸⣿⣴⣿⣿•⡀⣼⣿⣿⣸⣠⣿⣷•⣷⣿⣿⢀⣿⣿⣿⣿⣿⣷⣴⡀⣼⣄⣄⡄⡇⣿⣿⣼⣄⣸⣇⣿⢸⣿⣿⣿⣦⡇⣾⣿⣿⡀⣠⣸⡾│
 2┤⣤•••⣤⣤•••⣤•••⣦⣤⣄⣤⣤••⣤•••••⣤⣤⣤•⣧•⣤••••⣤•⣤•⣠⣤⣤⣠••⣇⣄⣤⣠•⣤⣤•⣼•••⣤••••⣠•⣀⣄│
  └┬──────────┬──────────┬───────────┬──────────┬──────────┬──────────┬┘
 14:05      14:25      14:45       15:05      15:25      15:45    16:05
RTT ms

               Bandwidth (Mbit/s) — passive, actual traffic
  ┌────────────────────────────────────────────────────────────────────┐
65┤ ⢕⢕ en0 down                  ⡆                                     │
52┤ ⢕⢕ en0 up                    ⡇                                     │
39┤ ⢕⢕ tailscale down            ⡇                ⡀       ⢀            │
26┤ ⢕⢕ tailscale up     ⢀        ⡇       ⢀        ⡇       ⢸            │
13┤        ⡇ ⢠ ⢀        ⣀  ⡆     ⡇  ⢀⢠ ⢀ ⢸⣀       ⡇       ⢠      ⡀     │
 0┤⣀⣀⣀⣀⣀⣀⣀⣀⣇⣀⣀⣀⣸⣀⣀⣀⣀⣀⣀⣀⣀⣿⣀⣀⣀⣀⣀⣀⣀⣀⣇⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣸⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀│
  └┬──────────┬──────────┬───────────┬──────────┬──────────┬──────────┬┘
 14:05      14:25      14:45       15:05      15:25      15:45    16:05
Mbit/s

smokelive redraws this live; smokekiosk drops legend/axes/title (subtle gray frame).
HTTP TTFB / mtr per-hop / WiFi RSSI+noise / iperf3 up+down panels stack below, same style.

CHANGELOG (newest first, all 2026-05-28):

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
  smoke [--panels …|--kiosk] [--minutes N|--hours N]      TUI (default: all panels)
  smokelive 24h [sec] | smokekiosk 24h [sec]              live / wall display
  smokepng [--width " --dpi N --panels …]                 PNG -> Preview
  config: env vars (SMOKEMON_*) in ~/Library/LaunchAgents/com.stefan.smokemon{,-probes,
          -iperf,-daily}.plist; labels/colors in term_plot.py & plot.py.
  jobs:   launchctl [bootout|bootstrap] gui/$(id -u) <plist>   (wait out the old one)
  deps:   fping, mtr, iperf3 (brew); curl/netstat/ifconfig/system_profiler (macOS);
          python3(anaconda)+matplotlib/numpy(PNG), plotext(TUI).
