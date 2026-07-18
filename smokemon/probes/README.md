# smokemon/probes

> the measurement layer. each probe is one stdlib python module that samples a single
> signal and hands it to the detector. nothing here daemonizes, streams, tails, or discovers
> broadly — every probe is bounded by an explicit interval, timeout and row/byte cap so the
> whole collector stays at ~30 mb rss and well under 1% of one core on a pi or jetson.

a probe is not a service, and it is not a decision-maker. it is a function the collector calls
on a schedule. the entire contract is one callable:

```python
def collect(conn) -> None:
    """sample one signal, hand the values to the detector. raise on hard failure."""
```

`smokemon.collect` owns the loop, the scheduling, the governor and the crash handling.
`smokemon.detect` owns every threshold, debounce and hysteresis decision. a probe just
samples. that split is the whole reason a misbehaving probe can be shed, retried, or recorded
as an error without touching any other signal — and the reason a threshold change never means
touching a probe.

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
  fast  = ping, net                          @ SMOKEMON_INTERVAL (10s)
  slow  = wifi, host, heartbeat, sweep,      @ SMOKEMON_PROBE_INTERVAL (60s) /
          baseline-flush                       HOST_INTERVAL (30s) / HEARTBEAT_INTERVAL (300s)
          + inventory (auto, 1h), logexcerpt (opt-in)
  all   = fast + slow (single thread; used for a one-process local test run)

production runs fast and slow as two separate services so a slow host probe can never delay a
ping cycle.
```

## what a probe feeds

a probe emits `(signal, entity, value)` triples. `signal` is the rule namespace; `entity` is
the per-instance discriminator, canonicalised by the probe so an interface alias or a mount-path
variant does not silently become a second signal with its own cold baseline.

```
ping.py    ping.loss/TARGET · ping.loss_run/TARGET · ping.rtt_med/TARGET
host.py    host.temp · host.mem · host.swap · host.psi_cpu · host.psi_io
           disk.used_pct/MOUNT · disk.inode_used_pct/MOUNT
net.py     net.err_rate/IFACE
wifi.py    wifi.rssi
```

the entity is the target **as configured**, not a resolved address: a name that resolves
somewhere else tomorrow must stay the same signal rather than becoming a second one with a cold
baseline. the rule table these feed is in
[../../docs/detector-spec.md](../../docs/detector-spec.md).

## the two footprint tiers

probes are classified by cost, and the collector schedules them accordingly. this is the
single most important design constraint — see the repo-root `AGENTS.md`.

```
always-on   ping, net, wifi, host
            cheap, stdlib or a single short subprocess; safe to run every cycle on the edge.

auto        inventory
            registered by default, delta-coded: one /proc + /sys scan per hour that usually
            emits zero rows. a node pays ~nothing for it.

opt-in      logexcerpt
            off unless explicitly configured, because it reads files. you turn it on; nothing
            turns it on for you.
```

## what every probe does — and refuses to do

`+` = what it samples; `-` = what it deliberately refuses to do; unmarked = how/with what.

```diff
# always-on probes — cheap enough to run every cycle on the edge
  ping.py     latency + packet loss via fping (-C count -p period -q)
+ feeds       loss% over the WHOLE fping cycle + median rtt, per target
  uses        fping (external) + statistics (stdlib); timeout = count*period/1000 + 30s
- avoids      no raw-socket ICMP of its own, no continuous ping, and never feeds a single
              ping as loss — one ping is only ever 0 or 100, which makes every threshold
              between them meaningless

  net.py      per-interface health
+ feeds       errors+drops per second per iface; relabels tailscale, cached 5 min. a counter
              that goes backwards is a reset (interface or box restarted), so it re-seeds
              rather than emitting a negative rate
  uses        adapters.read_net_counters() / read_net_errors() (/proc/net/dev)
- avoids      throughput is deliberately NOT a signal: high traffic is context, not an anomaly,
              and a z-rule over it would open an incident every time somebody downloaded
              something large

  wifi.py     wifi signal
+ feeds       rssi in dBm, with the rule's direction declared explicitly (lower is worse)
  uses        adapters.wifi_probe() (iw / /proc/net/wireless)
- avoids      silent no-op off-wifi or SMOKEMON_WIFI=0; no scanning of other APs; entity is
              empty rather than the SSID, so roaming between APs does not mint a cold baseline

  host.py     host health — the largest probe; internal fast/slow/vslow tiers in one cycle
+ fast        cpu/load/mem/swap/cache/oom, temp + per-zone thermal, PSI, cpu freq/throttle,
              disk IO + mounts, tcp/conntrack, jetson power+GPU, top-N procs
+ slow 5min   raspberry pi `vcgencmd get_throttled` bits (under-voltage / freq-cap / throttle)
+ vslow 1h    eMMC/SD wear-level + ioerr count (mmcblk life_time)
  self        keeps its last sample in memory for the heartbeat and the detector to read
              without re-probing, incl. smokemon's own RSS and SD write rate
  uses        pure /proc + /sys. jetson GPU from sysfs, NOT tegrastats
  events      edge/counter events off values already computed (oom/cpu-throttle/overtemp/
              swap/undervolt) — no extra probing
- avoids      no sudo (temp is NULL when unreadable, not root), no nvidia-smi/tegrastats,
              no per-process cmdline history
```

```diff
# auto probe — registered by default, near-zero steady-state cost
  inventory.py device + environment facts, delta-coded
+ samples      model/kernel/os/jetpack-l4t/cpu/mem/interfaces/gateway/boot-id; writes a
               device_facts row ONLY on change (steady state = 0 rows/hour)
  uses         /proc + /sys, platform
- avoids       no log streaming, no package inventory, no continuous polling of static facts
```

```diff
# opt-in probe — off unless configured (it reads files)
  logexcerpt.py event-driven, capped, redacted log tail — explicitly NOT a stream
+ samples       bounded tail of configured files, only on a warn/error+ event; per-file byte
                cursor, rotation reset, redaction, drop-oldest cap. links to the incident that
                triggered it by uid, so an excerpt is evidence rather than free-floating text
  uses          file reads + a node-local log_cursors table (not shipped) + sqlite
- avoids        OFF by default; no streaming, no full-file ship, no pre-existing history (the
                cursor seeds at EOF on first sight), no unredacted secrets
```

## what we avoid (and why), everywhere

these are not per-probe choices — they are the rules every probe in this directory obeys,
because smokemon runs on the edge (a pi/jetson on someone's link) and must never become the
problem it's monitoring. `-` = what we refuse to do; `+` = what we guarantee instead.

```diff
# the footprint contract every probe signs
- no time series on disk — a probe feeds the detector; normal samples live in memory and die there
- no log streaming — logs are event-gated, capped, redacted tails, never a tail -f / journal follow
- no broad discovery — explicitly-named or narrowly auto-detected targets; no sweeps, no port scans
- no persistent subprocesses — every CLI is one bounded subprocess.run with a timeout, nothing tailing
- no policy in a probe — thresholds, debounce and hysteresis belong to detect.py, never here
+ stdlib only on the edge — just the OS adapter, fping and iw
+ edge/counter events are free — derived from values already computed; fire once, clear on recovery
+ the governor can shed — over the RSS/DB budget, the probes it names get dropped for that cycle
```

## adding a probe

```
1. write smokemon/probes/<name>.py exposing collect(conn) -> None. sample, then call
   incidents.evaluate(conn, "<signal>", entity, value, ts) for each value. do not threshold,
   do not decide, do not store a series.
2. give the signal a kind in detect.SIGNAL_KINDS (gauge/latency/ratio/capacity/counter_rate/
   binary/state) — the kind decides whether a robust-z rule is meaningful at all — and a rule
   in detect.RULES if the generic z fallback is not right for it.
3. register it in collect.py: pick a tier (fast = cheap+always; slow = the rest), an interval,
   and gate it (auto self-detect, or opt-in behind a config flag). give it a name the governor
   can shed by.
4. respect the rules above: bounded timeout + row/byte caps, stdlib + a single bounded
   subprocess at most, no streaming/discovery, silent no-op when its dependency is absent.
5. wire any new env vars through config.py and document them in INSTALL.md; document the rule
   in docs/detector-spec.md.

a probe never talks to the hub, the shipper, or the UI directly. it samples and hands the value
over. that's all.
```
