# smokemon/probes

> the measurement layer. each probe is one stdlib python module that samples a single
> signal and writes rows to SQLite. nothing here daemonizes, streams, tails, or discovers
> broadly — every probe is bounded by an explicit interval, timeout and row/byte cap so the
> whole collector stays at ~30 mb rss and well under 1% of one core on a pi or jetson.

a probe is not a service. it is a function the collector calls on a schedule. the entire
contract is one callable:

```python
def collect(conn) -> None:
    """sample one signal, INSERT rows, conn.commit(). raise on hard failure."""
```

`smokemon.collect` owns the loop, the scheduling, the governor and the crash handling. a
probe just samples and stores. that split is the whole reason a misbehaving probe can be
shed, retried, or recorded as an error without touching any other signal.

```
== how a probe runs ==

collect.py builds a list of (interval, name, collect_fn) per group and hands them to the
scheduler. each fn is wrapped by _guarded():

  - governor first: over the RSS/DB budget? shed this cycle, log an event, skip the call.
  - crash isolation: a probe exception is caught, logged, and tripped as an `error` event
    (edge-triggered: fires once, clears on recovery). one probe crashing never kills the loop
    or the other probes.
  - DB lock contention (sqlite "busy"/"locked") is demoted to a single `warn` and never
    expedited — it's disk pressure, not a probe bug, so it must not cascade.

groups (see collect.py):
  fast  = ping, net                         @ SMOKEMON_INTERVAL (10s)
  slow  = http, mtr, wifi, host, ports      @ SMOKEMON_PROBE_INTERVAL (60s) / HOST_INTERVAL (30s)
          + synthetic, ext, redis, docker, pipeline, inventory, logexcerpt (when enabled)
  all   = fast + slow (single thread; used for a one-process local test run)

production runs fast and slow as two separate services so a slow http/mtr probe can never
delay a ping cycle. iperf and synthetic also run as their own timers (real bandwidth /
opt-in), not inside the collect loop.
```

## the three footprint tiers

probes are classified by cost, and the collector schedules them accordingly. this is the
single most important design constraint — see the repo-root `AGENTS.md`.

```
always-on   ping, net, http, mtr, wifi, host, ports
            cheap, stdlib or a single short subprocess; safe to run every cycle on the edge.

auto        redis, docker, pipeline, inventory
            registered by default but self-detect their dependency at collect time and stay
            a silent no-op when it's absent (no docker socket / no reachable redis / no gst
            process). a node that doesn't run the service pays ~nothing.

opt-in      iperf, synthetic, ext, logexcerpt
            off unless explicitly configured, because each one either spends real bandwidth,
            makes external requests, or reads files. you turn these on; nothing turns them on
            for you.
```

## what every probe does — and refuses to do

```
== always-on probes ==

ping.py        latency + packet loss via fping (-C count -p period -q).
  does         one ping_runs row per target per cycle + every individual rtt in ping_rtts.
               pre-aggregates min/p25/median/p75/mean/max/stddev (statistics.quantiles) at
               insert, so the percentile renderer reads ping_runs only and skips the rtt scan.
  uses         fping (external), statistics (stdlib). timeout = count*period/1000 + 30s.
  doesn't      no raw-socket ICMP of its own (no CAP_NET_RAW in python), no continuous ping.

net.py         per-interface bandwidth.
  does         cumulative ibytes/obytes/ipkts/opkts per iface; delta -> Mbit/s done at plot
               time. relabels the tailscale iface; that detection is cached for 5 min.
  uses         adapters.read_net_counters() (netstat / /proc/net/dev via the OS adapter).
  doesn't      stores counters, not rates — no per-sample arithmetic, no pcap, no per-flow.

http.py        http/s timing breakdown via curl HEAD.
  does         dns / tcp-connect / tls / ttfb / total in ms per URL (curl -w format string).
               emits an edge event per URL off the SAME request: 5xx or no-response trips a
               warn once, clears when it answers <500 again.
  uses         curl (external, -sI -o /dev/null --max-time 10), timeout 15s.
  doesn't      no GET body, no extra request for the health check, no following redirects for
               content — it's a timing probe, not a scraper.

mtr.py         per-hop route latency/loss via mtr --json.
  does         one mtr_hops row per hop per target: loss% / last / avg / best / worst / stddev.
  uses         mtr (external, needs root). macOS: sudo -n; Linux: setcap + SMOKEMON_MTR_SUDO=0.
               interval 0.2s as root, 1.0s otherwise (mtr forbids sub-second for non-root).
  doesn't      no traceroute fallback, no per-hop reverse-DNS (runs -n), no unbounded count.

wifi.py        wifi signal.
  does         rssi / noise / tx-rate / channel (+ bssid, retry/beacon counters on Linux).
  uses         adapters.wifi_probe() (system_profiler / iw / /proc/net/wireless per OS).
  doesn't      silent no-op when not on wifi or SMOKEMON_WIFI=0. no scanning of other APs.

host.py        host health — the largest probe. internal fast/slow/vslow tiers, one cycle:
  fast         cpu%, load, mem/swap/cache, oom_kill_count, temp (max zone), per-zone thermal,
               PSI cpu/mem/io, cpu freq + throttle counters, disk IO + mounts, tcp/udp +
               conntrack fill, jetson per-rail power (INA3221) + GPU util/freq (sysfs), top-N
               procs, and a self-footprint row.
  slow (5min)  raspberry pi `vcgencmd get_throttled` bits (under-voltage / freq-cap / throttle).
  vslow (1h)   eMMC/SD wear-level + ioerr count (mmcblk life_time).
  uses         Linux: pure /proc + /sys reads. macOS subset: sysctl, vm_stat, pmset -g therm,
               ioreg, netstat -s, ps. jetson GPU from sysfs/devfreq — NOT tegrastats.
  self         records its own 'smokemon' proc row every cycle (RSS summed across all smokemon
               pids on Linux, plus the SD write rate that actually wears the card), because the
               top-N sampler would miss a low-cpu daemon. this is what proves the ~30 mb claim.
  events       edge/counter events off values already computed (oom, cpu-throttle, overtemp,
               swap-high, pi under-voltage/throttle) — no extra probing, so no footprint.
  doesn't      no powermetrics/sudo on macOS (temp stays NULL rather than demand root), no
               nvidia-smi/tegrastats process, no per-process cmdline history.

ports.py       per-port connection counts + byte volume, no root.
  does         /proc/net/{tcp,tcp6,udp,udp6} for listen ports + conn/peer counts; SOCK_DIAG
               netlink (tcp_info bytes_acked/received) for per-port byte volume. inbound =
               local LISTEN ports; outbound = remote service ports, grouped so thousands of
               ephemeral client ports collapse to one row. capped at 80 rows.
  uses         socket + struct (stdlib). tcp_info is readable unprivileged.
  doesn't      no per-connection rows, no DPI, no remote-host resolution; bytes are a gauge
               (cumulative per open connection), not a clean rate.
```

```
== auto probes (default on, no-op when the dependency is absent) ==

redisq.py      redis stream / queue health via the RESP wire protocol.
  does         one short-lived TCP socket: PING, INFO memory/clients/stats, XLEN per explicit
               stream, optional XPENDING per stream=group. records mem, ops/sec, evicted/
               rejected, connected/blocked clients, stream depth + pending.
  uses         socket (stdlib). bounded by SMOKEMON_REDIS_TIMEOUT.
  doesn't      no redis-py dependency, no redis-cli subprocess, no docker/log inspection.
               auto: a redis that has never answered is treated as "not present" and stays
               silent; a down row is recorded only once it has answered before, or when forced.

dockerps.py    container health via the Engine API over its unix socket.
  does         one bounded HTTP/1.0 `GET /containers/json?all=1` per cycle -> state, health,
               exit code. optional small per-container inspect (restart_count / oom / health)
               and optional cgroup-v2 /sys reads (live cpu% / mem / pids).
  uses         socket AF_UNIX + json (stdlib). bounded by DOCKER_TIMEOUT / MAX_BYTES / MAX.
  doesn't      no docker CLI, no `docker logs`, no event stream, no journal/log tailing. the
               daemon up/down row is EDGE-triggered so an unreachable socket can't spam.

pipeline.py    pipeline / process liveness (gstreamer + rtsp).
  does         one /proc scan matching cmdline substrings (auto-watches gst-launch) -> count,
               summed cpu/rss, youngest-process uptime, restart count (starttime moved).
               one bounded RTSP OPTIONS per endpoint confirms a stream is actually served;
               endpoints are auto-discovered from rtsp:// inside watched cmdlines (cap 16).
  uses         os.scandir(/proc) + socket (stdlib).
  doesn't      no `ps`, no ffprobe/gst subprocess, no media bytes read, no unbounded fan-out.

inventory.py   device + environment facts, delta-coded.
  does         model / kernel / os release / jetpack-l4t / cpu count / mem / interfaces /
               gateway / boot id / tailscale iface. writes a device_facts row ONLY when a
               value changes — steady state is one /proc+/sys scan an hour that emits 0 rows.
  uses         /proc + /sys reads, platform, a couple of sysctl reads on macOS.
  doesn't      no log streaming, no package inventory, no continuous polling of static facts.
```

```
== opt-in probes (off unless configured) ==

iperf.py       active throughput, up + down, to a peer running `iperf3 -s`.
  does         up_mbps / down_mbps / retransmits + rtt_under_load_ms (mean tcp rtt while the
               pipe is saturated) — paired with the idle ping baseline that's the bufferbloat
               grade. guards partial JSON from a server-timed-out run.
  uses         iperf3 (external, -J --connect-timeout 5000), timeout duration+30s.
  doesn't      SPENDS REAL BANDWIDTH, so it runs sparsely on its own timer, never in the loop;
               no-ops entirely if SMOKEMON_IPERF_SERVER is unset.

synthetic.py   scripted multi-step checks beyond single-shot ping/http.
  does         captive-portal / interception detection (the 204-no-content check) + a
               DNS-over-HTTPS resolution check (RFC 8484 json). pass/fail + latency + detail.
               classification helpers are pure (no socket) so they unit-test offline.
  uses         urllib (stdlib). opt-in via SMOKEMON_SYNTHETIC=1.
  doesn't      makes no extra external request unless explicitly enabled.

ext.py         lightweight external http scrapes with a hard footprint budget.
  does         reads explicit health/metrics endpoints only: always emits up + latency_ms;
               json contributes numeric/bool fields; openmetrics contributes ONLY metrics on
               an explicit per-endpoint allowlist.
  uses         urllib (stdlib). bounded: EXT_TIMEOUT (2s), EXT_MAX_BYTES (256 KiB),
               EXT_MAX_METRICS (20), depth-5 json flattening.
  doesn't      no log tailing, no docker/journal scans, no persistent subprocesses, no
               unbounded body, no scraping anything not explicitly listed.

logexcerpt.py  event-driven, capped, redacted log tail — explicitly NOT a stream.
  does         ships a bounded tail of configured files, and only when a warn/error+ event
               just landed. byte-offset cursor per file (never ships the same bytes twice),
               rotation/truncation reset, secret redaction, hard per-excerpt byte cap with
               drop-oldest. seeds to EOF on first sight so enabling it never dumps history.
  uses         file reads + a node-local log_cursors table (not shipped) + sqlite.
  doesn't      OFF by default. no streaming, no full-file ship, no pre-existing history, no
               unredacted secrets. steady state with no incidents = one cheap query, zero reads.
```

## what we avoid (and why), everywhere

these are not per-probe choices — they are the rules every probe in this directory obeys,
because smokemon runs on the edge (a pi/jetson on someone's link) and must never become the
problem it's monitoring.

```
no log streaming            logs are event-gated, capped, redacted tails — never a tail -f or
                            a journal follow. a noisy box must not flood disk or the wire.
no docker/journal log scans the docker and pipeline probes read state/metadata only; they
                            never call `docker logs` or scan a journal.
no broad discovery          probes watch explicitly-named (or narrowly auto-detected) targets.
                            no subnet sweeps, no port scans, no "find every service".
no large scrape bodies      ext/docker/redis all cap response bytes and metric counts; an
                            endpoint that returns megabytes is truncated, not ingested.
no persistent subprocesses  every external tool (fping/mtr/curl/iperf3/vcgencmd) is a single
                            bounded subprocess.run with a timeout — nothing is left tailing.
stdlib only for collection  the only non-stdlib deps are the external CLIs above and the OS
                            adapter; no requests/redis-py/docker-py on the edge.
auto-probes self-detect     a missing dependency is a silent no-op, not an error and not a
                            retry storm — a node only pays for the services it actually runs.
edge/counter events are free they derive from values a probe already computed in the same
                            cycle, fire once on transition and clear on recovery — no re-probe.
the governor can shed        when RSS or the DB exceed their budget, the collector drops the
                            expensive probes (mtr/synthetic/ext) for that cycle and logs it.
```

## adding a probe

```
1. write smokemon/probes/<name>.py exposing collect(conn) -> None. sample, INSERT, commit.
2. declare its table(s) in schema.py (single-source DDL -> node + hub + STD_TABLES).
3. register it in collect.py: pick a tier (fast = cheap+always; slow = the rest), an interval,
   and gate it (auto self-detect, or opt-in behind a config flag). give it a name the governor
   can shed by.
4. respect the rules above: bounded timeout + row/byte caps, stdlib + a single bounded
   subprocess at most, no streaming/discovery, silent no-op when its dependency is absent.
5. wire any new env vars through config.py and document them in INSTALL.md.

the renderers (render/tui, render/png) and the hub read whatever lands in the tables; a probe
never talks to the hub, the shipper, or the UI directly. it samples and stores. that's all.
```
