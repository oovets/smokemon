# Changelog

All notable changes to smokemon. The format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **macOS implementations** for thermal / power / tcp panels (previously Linux-only):
  - `thermal`: `pmset -g therm` -> `cpu_speed_limit_pct` pseudo-zone (100 = no thermal throttling, less means the kernel is capping clock speed for heat).
  - `power`: `ioreg -rc AppleSmartBattery` -> single `battery` rail with watts / volts / amps. Empty on desktops (no battery).
  - `tcp`: `netstat -s -p tcp` and `-p udp` parsed for retransmits, RSTs, rexmit drops, UDP bad checksums, and UDP no-socket drops. Conntrack remains None (pf state count needs root).
  - `host` panel also gets `swap_used_pct` (from `sysctl vm.swapusage`) and `cache_mb` (from `vm_stat` file-backed pages) on macOS.
- **Linux-only by design**: `psi` (no equivalent on macOS without `sudo powermetrics`) and `freq` (Apple Silicon does not expose per-core clock speed without sudo).

## [0.11.0] - 2026-05-28

### Added

- **PSI metrics**: `psi_cpu`, `psi_mem`, `psi_io` (10-second rolling averages from `/proc/pressure/*`) in `host_samples`. Early-warning signal for latency before resources hit 100%.
- **Memory pressure**: `swap_used_pct`, `cache_mb`, `oom_kill_count` in `host_samples`.
- **CPU frequency + throttle**: `cpu_freq_mhz`, `cpu_throttle_count`, `pi_throttle_bits` in `host_samples`. Detects silent perf regressions (100% busy at 600 MHz looks the same as at 1500 MHz). The Pi bit field (from `vcgencmd get_throttled`) is sampled on a 5-minute slow tier.
- **Thermal zones table** `thermal_zones (ts, zone, temp_c)`: every zone sampled individually (Jetson has ~10). The legacy `temp_c` column in `host_samples` still carries the max-of-zones for back-compat.
- **Power rails table** `power_samples (ts, rail, watts, volts, amps)`: Jetson INA3221 i2c readings (`/sys/bus/i2c/drivers/ina3221x/*`).
- **TCP/UDP/conntrack table** `tcp_samples`: kernel counters from `/proc/net/snmp` + conntrack fill from `/proc/sys/net/netfilter/nf_conntrack_*`.
- **Disk health table** `disk_health`: SD/eMMC wear-level (`/sys/block/mmcblk*/device/life_time`) on a 60-minute very-slow tier.
- **WiFi extras**: `bssid`, `retry_count`, `discard_count`, `beacon_loss` in `wifi_samples`. Roams across BSSIDs are summarised in the render.
- **Inode usage**: `inode_used_pct` in `disk_samples`.
- **Grid layout for plots**: PNG and TUI auto-arrange panels in a 2-column grid when there are >=3 panels and the canvas is wide enough. `--cols N` forces a specific count.
- **Five new panels**: `thermal`, `power`, `tcp`, `psi`, `freq`. All optional and selected via `--panels`.
- **Migration**: `ensure_body_columns()` ALTERs in any missing body columns on existing tables, so upgrades from older DBs are transparent (old rows get NULL for new columns).

### Changed

- **Ping percentiles pre-aggregated**: `rtt_p25` and `rtt_p75` are computed at insert time. `load_ping_smoke` reads them straight from `ping_runs` instead of scanning `ping_rtts`. Old rows fall back to a JOIN-based percentile calculation against a temp-id table (no IN-list variable limit).
- **Hub ingest** uses `executemany` for all non-ping tables (per-row execute is kept only for `ping_runs` where `lastrowid` is needed for the run_map). ~30-40% faster ingest on Pi-class hardware.
- **`load_net` uses SQL `LAG()`** for in-database delta computation when SQLite >= 3.25; falls back to the Python loop otherwise.
- **`host._procs_linux`** uses `os.scandir("/proc")` instead of `os.listdir`.
- **`probes/http.py`** caches the `curl` path at import (was resolved on every probe).

## [0.10.0] - 2026-05-28

### Changed

- **Package refactor**: flat scripts collapsed into a `smokemon/` package - `config` (env/NODE/paths), `core` (log/connect/signals/run_scheduler), `schema` (single-source DDL, generic insert), `adapters/{darwin,linux}`, `probes/{ping,net,http,mtr,wifi,iperf,host}`, `collect` (one daemon, group fast|slow|all), `ship`, `hub`, `query` (shared loaders + `--node`), `render/{tui,png}`, `cli` (`smoke` subcommands).
- 3 collector daemons collapsed to 2 (`fast`=ping/net; `slow`=http/mtr/wifi/host). `live.sh`/`daily_graph.sh` replaced by `smoke live`/`smoke kiosk`/`smoke daily`.
- Deduplication: schema, daemon loop, plot loaders, and a duplicate `wifi_probe` all consolidated. Net caches the Tailscale interface for 5 minutes. Hub uses `ThreadingHTTPServer` + write lock.
- Entry points: `python -m smokemon.*` (PYTHONPATH=repo, no install needed).

## [0.9.0] - 2026-05-28

### Added

- **Cross-platform adapters** (`platform.system()` dispatch): Linux paths use `/proc/net/dev`, `tailscale0` (or `100.64.0.0/10` scan via `ip`), `iw dev` + `/proc/net/wireless`. macOS paths unchanged. CLI tool paths resolved via env -> `shutil.which`.
- **Node dimension**: every table gains a `node` column (defaults to hostname, override via `SMOKEMON_NODE`). Additive migration (ALTER + backfill) so node and hub DBs share one schema -> one plotter codebase.
- **Host health collector** @30s. Linux: cpu (`/proc/stat`), load, mem (`/proc/meminfo`), temp (`/sys/class/thermal`, incl. Jetson), disk used (`statvfs`) + IO (`/proc/diskstats`), top-N procs (`/proc/<pid>/stat`). macOS subset. New tables: `host_samples`, `disk_samples`, `proc_samples`.
- **Central aggregation (push)**: shipper drains new rows per table (delta by id, cursor in `ship_state`) and POSTs to the hub. Hub writes `smokemon-hub.db` with `node` + `src_id`, `UNIQUE(node, src_id)` + `INSERT OR IGNORE` in one transaction = idempotent. `ping_rtts` remapped to hub run ids, inserted only for newly-inserted runs. Shared-secret header `X-Smokemon-Key`.
- **Plotters**: `--node` filter (required on hub DB). Host + disk panels. matplotlib stays hub-only so nodes need only python3 stdlib + `plotext` (TUI).
- **Linux deploy**: `install.sh` (apt deps, `setcap cap_net_raw` on fping/mtr to skip sudo, `/etc/smokemon.env`, systemd units). macOS launchd plists.

## [0.8.0] - 2026-05-28

### Changed

- **PNG width scales with time span** (~2 inches per hour, clamp 16-80"). Every 10s sample stays horizontally distinguishable. dpi lowered 130 -> 96 (granularity from width, not pixel density). 24h = ~4608xN px (~1 MB). New flags: `--width`, `--dpi`.

## [0.7.0] - 2026-05-28

### Changed

- X ticks formatted `%H:%M` (never seconds), Y integer ticks. HTTP labels strip TLD (`cloudflare.com` -> `cloudflare`). Loss marker switched from `X` to braille dot. HTTP lines use a fixed non-red palette (cyan/green/magenta) so they cannot be confused with red loss. Kiosk keeps a subtle gray frame, titles off.

## [0.6.0] - 2026-05-28

### Added

- **Kiosk mode** (`term_plot --kiosk`, `smokekiosk`): no legend/ticks/labels/title/header.

## [0.5.0] - 2026-05-28

### Added

- **Active probes @60s**: HTTP timing via `curl -sI -w` (DNS/connect/TLS/TTFB/total), `mtr --json -c10` for per-hop loss/avg/best/worst/stddev, WiFi RSSI/noise/tx/channel via `system_profiler`. mtr requires passwordless sudo (or `setcap cap_net_raw` on Linux).
- **iperf3 probe @900s**: `-J` (up) + `-R` (down). Consumes real bandwidth; requires `iperf3 -s` on the server.
- **New tables**: `http_samples`, `mtr_hops`, `wifi_samples`, `iperf_samples`. 6 panel types.

## [0.4.0] - 2026-05-28

### Added

- Live window units (`Nh`/`Nm`/bare-number minutes): `smokelive 24h 30`.

## [0.3.0] - 2026-05-28

### Added

- Third ping target reachable over the Tailscale interface. Tailscale interface auto-detected as the one with an address in `100.64.0.0/10`; label `tailscale` (survives utun renumbering across reboots).

### Fixed

- `netstat -ibn` rows for MAC-less interfaces (utun) have 10 fields, not 11. The old code indexed columns wrong and silently skipped them; now we read the last 7 columns. utun was never captured before this.

## [0.2.0] - 2026-05-28

### Added

- **TUI**: plotext braille plot (replaced a chafa inline-image PoC).
- **Live + scheduled**: `live.sh`, `daily_graph.sh` + launchd `StartCalendarInterval` at 23:55 -> `graphs/daily/`. Zsh helpers `smoke`/`smokelive`/`smokepng`.

## [0.1.0] - 2026-05-28

### Added

- **Core collector @10s**: `fping -C20 -p50` (latency/loss + every individual RTT) + `netstat -ibn` (cumulative bytes -> Mbit/s via delta/dt). Default targets `1.1.1.1`, `192.168.0.1`.
- **SQLite WAL**: `ping_runs`, `ping_rtts`, `net_samples`. No pruning (~5-6 GB/year; long-term plan is rollup/compression, never deletion of raw data).
- **PNG renderer**: matplotlib smoke-style plot (fill_between p0-p100 + p25-p75 + median + loss scatter).

[Unreleased]: https://github.com/oovets/smokemon/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/oovets/smokemon/releases/tag/v0.11.0
[0.10.0]: https://github.com/oovets/smokemon/releases/tag/v0.10.0
[0.9.0]: https://github.com/oovets/smokemon/releases/tag/v0.9.0
[0.8.0]: https://github.com/oovets/smokemon/releases/tag/v0.8.0
[0.7.0]: https://github.com/oovets/smokemon/releases/tag/v0.7.0
[0.6.0]: https://github.com/oovets/smokemon/releases/tag/v0.6.0
[0.5.0]: https://github.com/oovets/smokemon/releases/tag/v0.5.0
[0.4.0]: https://github.com/oovets/smokemon/releases/tag/v0.4.0
[0.3.0]: https://github.com/oovets/smokemon/releases/tag/v0.3.0
[0.2.0]: https://github.com/oovets/smokemon/releases/tag/v0.2.0
[0.1.0]: https://github.com/oovets/smokemon/releases/tag/v0.1.0
