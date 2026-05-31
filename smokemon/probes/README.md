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

`+` = what it samples; `-` = what it deliberately refuses to do; unmarked = how/with what.

```diff
# always-on probes — cheap enough to run every cycle on the edge
  ping.py     latency + packet loss via fping (-C count -p period -q)
+ samples     1 ping_runs row/target/cycle + every rtt; pre-aggregates min/p25/median/p75/mean/max/stddev at insert
  uses        fping (external) + statistics (stdlib); timeout = count*period/1000 + 30s
- avoids      no raw-socket ICMP of its own (no CAP_NET_RAW in python), no continuous ping

  net.py      per-interface bandwidth
+ samples     cumulative ibytes/obytes/ipkts/opkts per iface (delta -> Mbit/s at plot time); relabels tailscale, cached 5 min
  uses        adapters.read_net_counters() (netstat / /proc/net/dev)
- avoids      stores counters not rates; no per-sample math, no pcap, no per-flow accounting

  http.py     http/s timing breakdown via curl HEAD
+ samples     dns / connect / tls / ttfb / total ms per URL; an edge event off the SAME request (5xx/no-response trips warn once, clears <500)
  uses        curl (external, -sI -o /dev/null --max-time 10), timeout 15s
- avoids      no GET body, no extra request for the health check, no redirect-following — a timing probe, not a scraper

  mtr.py      per-hop route latency/loss via mtr --json
+ samples     1 mtr_hops row/hop/target: loss% / last / avg / best / worst / stddev
  uses        mtr (external, root: macOS sudo -n / Linux setcap); interval 0.2s root, 1.0s otherwise
- avoids      no traceroute fallback, no per-hop reverse-DNS (runs -n), no unbounded count

  wifi.py     wifi signal
+ samples     rssi / noise / tx-rate / channel (+ bssid, retry/beacon counters on Linux)
  uses        adapters.wifi_probe() (system_profiler / iw / /proc/net/wireless)
- avoids      silent no-op off-wifi or SMOKEMON_WIFI=0; no scanning of other APs

  host.py     host health — the largest probe; internal fast/slow/vslow tiers in one cycle
+ fast        cpu/load/mem/swap/cache/oom, temp + per-zone thermal, PSI, cpu freq/throttle, disk IO + mounts, tcp/conntrack, jetson power+GPU, top-N procs
+ slow 5min   raspberry pi `vcgencmd get_throttled` bits (under-voltage / freq-cap / throttle)
+ vslow 1h    eMMC/SD wear-level + ioerr count (mmcblk life_time)
  self        its own 'smokemon' proc row each cycle (RSS summed over all smokemon pids + SD write rate) — proves the ~30 mb claim
  uses        Linux: pure /proc + /sys. macOS subset: sysctl, vm_stat, pmset, ioreg, netstat -s, ps. jetson GPU from sysfs, NOT tegrastats
  events      edge/counter events off values already computed (oom/cpu-throttle/overtemp/swap/undervolt) — no extra probing
- avoids      no powermetrics/sudo on macOS (temp NULL, not root), no nvidia-smi/tegrastats, no per-process cmdline history

  ports.py    per-port connection counts + byte volume, no root
+ samples     /proc/net/{tcp,udp}* listen ports + conn/peer counts; SOCK_DIAG netlink (tcp_info bytes) per port; capped at 80 rows
  uses        socket + struct (stdlib); tcp_info is readable unprivileged
- avoids      no per-connection rows, no DPI, no remote-host resolution; bytes are a gauge, not a clean rate
```

```diff
# auto probes — registered by default, a silent no-op when the dependency is absent
  redisq.py    redis stream / queue health via the RESP wire protocol
+ samples      one short-lived TCP socket: PING, INFO memory/clients/stats, XLEN, optional XPENDING; mem/ops/evicted/clients/depth/pending
  uses         socket (stdlib), bounded by SMOKEMON_REDIS_TIMEOUT
- avoids       no redis-py, no redis-cli subprocess, no docker/log inspection; stays silent until a redis has answered at least once

  dockerps.py  container health via the Engine API over its unix socket
+ samples      one bounded HTTP/1.0 GET /containers/json?all=1 -> state/health/exit; optional inspect (restart/oom) + cgroup-v2 cpu/mem/pids
  uses         socket AF_UNIX + json (stdlib), bounded by DOCKER_TIMEOUT / MAX_BYTES / MAX
- avoids       no docker CLI, no `docker logs`, no event stream, no journal tailing; daemon up/down row is edge-triggered

  pipeline.py  pipeline / process liveness (gstreamer + rtsp)
+ samples      one /proc scan matching cmdlines (auto gst) -> count/cpu/rss/uptime/restarts; one bounded RTSP OPTIONS per endpoint (auto-found, cap 16)
  uses         os.scandir(/proc) + socket (stdlib)
- avoids       no `ps`, no ffprobe/gst subprocess, no media bytes read, no unbounded fan-out

  inventory.py device + environment facts, delta-coded
+ samples      model/kernel/os/jetpack-l4t/cpu/mem/interfaces/gateway/boot-id; writes a row ONLY on change (steady state = 0 rows/hour)
  uses         /proc + /sys, platform, a couple of sysctl reads on macOS
- avoids       no log streaming, no package inventory, no continuous polling of static facts
```

```diff
# opt-in probes — off unless configured (each spends bandwidth, makes external requests, or reads files)
  iperf.py      active throughput up + down to a peer running `iperf3 -s`
+ samples       up/down Mbit/s + retransmits + rtt_under_load_ms (loaded TCP rtt) -> bufferbloat grade vs the idle ping baseline
  uses          iperf3 (external, -J --connect-timeout 5000), timeout duration+30s
- avoids        SPENDS REAL BANDWIDTH -> runs sparsely on its own timer, never in the loop; no-ops if SMOKEMON_IPERF_SERVER is unset

  synthetic.py  scripted multi-step checks beyond single-shot ping/http
+ samples       captive-portal / interception (204-no-content) + DoH resolution (RFC 8484); pure classifiers unit-test offline
  uses          urllib (stdlib), opt-in via SMOKEMON_SYNTHETIC=1
- avoids        makes no extra external request unless explicitly enabled

  ext.py        lightweight external http scrapes with a hard footprint budget
+ samples       explicit health/metrics endpoints only: up + latency_ms always; json numeric fields; openmetrics ONLY on an allowlist
  uses          urllib (stdlib); bounded EXT_TIMEOUT 2s / MAX_BYTES 256 KiB / MAX_METRICS 20 / depth-5 flatten
- avoids        no log tailing, no docker/journal scans, no persistent subprocesses, no unbounded body, nothing not listed

  logexcerpt.py event-driven, capped, redacted log tail — explicitly NOT a stream
+ samples       bounded tail of configured files, only on a warn/error+ event; per-file byte cursor, rotation reset, redaction, drop-oldest cap
  uses          file reads + a node-local log_cursors table (not shipped) + sqlite
- avoids        OFF by default; no streaming, no full-file ship, no pre-existing history, no unredacted secrets
```

## what we avoid (and why), everywhere

these are not per-probe choices — they are the rules every probe in this directory obeys,
because smokemon runs on the edge (a pi/jetson on someone's link) and must never become the
problem it's monitoring. `-` = what we refuse to do; `+` = what we guarantee instead.

```diff
# the footprint contract every probe signs
- no log streaming — logs are event-gated, capped, redacted tails, never a tail -f / journal follow
- no docker/journal log scans — docker & pipeline read state/metadata only, never `docker logs`
- no broad discovery — explicitly-named or narrowly auto-detected targets; no sweeps, no port scans
- no large scrape bodies — ext/docker/redis cap response bytes + metric counts; megabytes are truncated
- no persistent subprocesses — every CLI is one bounded subprocess.run with a timeout, nothing tailing
+ stdlib only on the edge — no requests/redis-py/docker-py; just the OS adapter + the external CLIs
+ auto-probes self-detect — a missing dependency is a silent no-op, never an error or a retry storm
+ edge/counter events are free — derived from values already computed; fire once, clear on recovery
+ the governor can shed — over the RSS/DB budget, the expensive probes (mtr/synthetic/ext) get dropped
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
