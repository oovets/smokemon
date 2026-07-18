# Detector specification

The operational reference for the node-side detector: `signals.py`, `baseline.py`,
`detect.py`, `incidents.py`.

For *why* it works this way, see [`adr/0001-incident-pivot.md`](adr/0001-incident-pivot.md).
This document is the *what*: exact thresholds, exact transitions, exact defaults.

---

## The signal registry

A **signal** is a named scalar series a probe produces as a side effect of what it already
computes: `(name, entity, value)`.

* `name` is the rule namespace — `ping.loss`, `host.temp`, `disk.used_pct`.
* `entity` is the per-instance discriminator — the ping target, the mount point, the interface.
  The probe canonicalises it, so an interface alias or a mount-path variant does not silently
  become a second signal with its own cold baseline.

Each `(name, entity)` gets a fixed-capacity ring of `(wall, mono, value)` triples. This is the
only place a sample lives while things are normal, and it is never written to disk.

### The memory bound, with the arithmetic

```
SIGNAL_MAX × SIGNAL_RING × 3 slots × 8 bytes
      48    ×      64    ×    3    ×    8     =  73 728 bytes  (72 KB)
```

Plus about 200 bytes per signal for the dict entry, the `Ring` object and three `array` headers:
`48 × 200 = 9 600 bytes`. Steady-state ceiling ≈ **83 KB**.

The three slots are three parallel `array('d')` — 8 bytes per element with no per-element Python
object — rather than a deque of tuples, which would cost roughly 120 bytes per `(wall, value)`
pair. That is the difference between ~83 KB and ~250 KB of RSS, and RSS is a number smokemon
publishes about itself. The observer must not move the thing it measures.

The bound is enforced **in `signals.feed()`**, not trusted upstream: once `SIGNAL_MAX` distinct
signals exist, feeding a new one is dropped. A node churning container names or interface
aliases therefore cannot grow the registry. Dropping is a real loss of coverage, so
`signals.drops()` is carried in the heartbeat and `should_warn_drops()` logs at most hourly —
a node in that state drops every cycle, and an unthrottled warning would itself become the
flood.

`SIGNAL_RING` (64) is roughly 10× `INCIDENT_PRE_SAMPLES` (6), so the same ring serves both the
pre-incident evidence window and debounce evaluation without a second buffer.

---

## Signal kinds

The kind decides whether a robust-z rule is meaningful at all.

| kind | z-eligible | why |
|---|---|---|
| `gauge` | yes | continuous, has meaningful spread |
| `latency` | yes | continuous, has meaningful spread |
| `ratio` | yes | continuous, has meaningful spread |
| `capacity` | no | absolute thresholds are the meaningful ones; z adds nothing to "disk is 92% full" |
| `counter_rate` | no | zero-inflated and bursty; z produces confident nonsense |
| `binary` | no | `{0,1}` has no useful spread |
| `state` | no | not ordered; a z-score over it is meaningless |

`Z_ELIGIBLE = {gauge, latency, ratio}`. The restriction applies to the **generic fallback
rule**, which is z-only. An explicit rule may still use absolute thresholds on any kind — that
is how `disk.used_pct` (capacity) and `net.err_rate` (counter_rate) are monitored.

**Consequence worth knowing:** an unregistered signal whose kind is not z-eligible falls back
to `FALLBACK._replace(trip_z=None, clear_z=None)` — which has no absolute threshold either, so
it can never trip. A new counter/binary/state signal is effectively unmonitored until someone
gives it an explicit rule. This is deliberate (better silent than confidently wrong) but it is
not obvious from the outside.

---

## The state machine

Five states. `OK`, `ARMED`, `OPEN`, `CLOSING`, `COOLDOWN`.

```
                 breach                for_s elapsed
        OK  ──────────────▶  ARMED  ──────────────────▶  OPEN
        ▲                      │                        │  ▲
        │       !breach        │                !breach  │  │ breach
        └──────────────────────┘                        ▼  │
                                                    CLOSING─┘
        ▲                                               │
        │  cooldown_s elapsed                           │ clearing, held clear_for_s
        │                                               ▼
        └───────────────────  COOLDOWN  ◀───────────────┘
                                  │
                                  └── breach ──▶ OPEN  (no ARMED phase)
```

### Transition table

| from | condition | to | action written |
|---|---|---|---|
| `OK` | breaching | `ARMED` | — (nothing is written) |
| `OK` | not breaching | `OK` | — (**the one place the baseline learns**) |
| `ARMED` | not breaching | `OK` | — (**nothing is written; this is the flap filter**) |
| `ARMED` | breaching, `mono − since ≥ for_s` | `OPEN` | `open` |
| `ARMED` | breaching, hold not elapsed | `ARMED` | — |
| `OPEN` | breaching | `OPEN` | `sample` (phase `during`) |
| `OPEN` | not breaching | `CLOSING` | `sample` (phase `during`) |
| `CLOSING` | breaching | `OPEN` | `sample` (phase `during`) — **flap absorber: a bouncing signal is ONE incident** |
| `CLOSING` | clearing, `mono − since ≥ clear_for_s` | `COOLDOWN` | `close` |
| `CLOSING` | not clearing (inside the hysteresis band) | `CLOSING` | — (hold timer **reset**) |
| `CLOSING` | clearing, hold not elapsed | `CLOSING` | — |
| `COOLDOWN` | breaching | `OPEN` | `open` (see reopen policy) |
| `COOLDOWN` | `mono − since ≥ cooldown_s` | `OK` | — |

Plus two age-based transitions from `sweep()`, which only considers `OPEN` and `CLOSING`:

| from | condition | to | action |
|---|---|---|---|
| `OPEN`/`CLOSING` | `now_mono − last_mono ≥ SIGNAL_STALE_S` | `COOLDOWN` | `stale` |
| `OPEN`/`CLOSING` | `now_wall − opened_wall ≥ INCIDENT_MAX_OPEN_S`, rule is absolute | `COOLDOWN` | `persist` |
| `OPEN`/`CLOSING` | `now_wall − opened_wall ≥ INCIDENT_MAX_OPEN_S`, rule is relative | `COOLDOWN` | `expire` + `baseline.thaw()` |

The `stale` path exists because without it a probe that dies leaves its incident open forever,
and the hub cannot tell that from a genuine ongoing fault.

### Actions → stored transitions

`detect` emits `Action`s; `incidents.py` maps them onto rows in the `incidents` table:

| action `op` | stored `transition` |
|---|---|
| `open` | `open`, or `reopen` inside the reopen window |
| `close` | `close` |
| `stale` | `stale` |
| `expire` | `expired` |
| `persist` | `persistent` |
| `sample` | (no transition row; writes `incident_samples` if the decimation ladder keeps it) |

`open`, `reopen` and `persist` carry the rule's severity. Every other transition carries
`info`: it reports the *end*, not the fault. `query.load_incidents` is written to preserve the
opening severity for exactly this reason — otherwise every closed incident would read as info.

### Trip and clear tests

**Trip** is absolute **OR** z, never AND (`detect._breach`). The z-score is the addition that
gives per-node context, not a second hurdle. A rule with one side `None` degenerates cleanly to
the other.

**Clear** must be clear on **both** axes (`detect._clearing`) — the negation of the OR. A signal
back under its absolute threshold but still 4σ from this node's normal is not yet recovered.

Direction is per rule: `+` means high is bad, `-` means low is bad. `wifi.rssi` is the case that
proves the flag is needed — RSSI is dBm, so −50 is good and −90 is bad, and without an explicit
`direction="-"` it would inherit the fallback's `+` and open an incident every time reception
*improved*.

"Worst" is likewise per rule (`peak_mode`): `max` for latency, `min` for RSSI, `max_abs_z` for
a signal whose extremity is best measured in σ. The stored row carries the mode so a reader
never has to guess. The running extremum is tracked over **every** sample seen, including ones
decimation discarded — the worst moment of an incident is frequently in a sample that was not
kept — and is re-persisted on every sample so it survives a restart.

---

## The rule table

Generated from `detect.RULES` so it cannot drift from the code:

```sh
python3 -c 'from smokemon import detect; [print(r) for r in detect.RULES.values()]'
```

`tests/test_docs_rules.py` asserts this table matches `detect.RULES`.

| signal              | kind         | abs | dir | trip | clear | trip_z | clear_z | for_s | clear_for_s | cooldown_s | sev   | peak | abs_floor | rel_floor | min_n | dyn |
|---------------------|--------------|-----|-----|------|-------|--------|---------|-------|-------------|------------|-------|------|-----------|-----------|-------|-----|
| ping.loss           | ratio        | yes | +   | 10   | 1     | -      | -       | 20    | 60          | 300        | error | max  | 0         | 0         | 30    | -   |
| ping.loss_run       | ratio        | yes | +   | 99.5 | 50    | -      | -       | 20    | 60          | 300        | crit  | max  | 0         | 0         | 30    | -   |
| ping.rtt_med        | latency      | no  | +   | -    | -     | 4      | 2       | 20    | 90          | 300        | warn  | max  | 2         | 0.05      | 30    | rtt |
| host.temp           | gauge        | yes | +   | 75   | 70    | -      | -       | 60    | 180         | 600        | warn  | max  | 0         | 0         | 30    | -   |
| host.mem            | gauge        | yes | +   | 92   | 80    | -      | -       | 120   | 300         | 900        | warn  | max  | 0         | 0         | 30    | -   |
| host.swap           | gauge        | yes | +   | 25   | 10    | -      | -       | 300   | 600         | 1800       | warn  | max  | 0         | 0         | 30    | -   |
| disk.used_pct       | capacity     | yes | +   | 92   | 85    | -      | -       | 300   | 900         | 3600       | error | max  | 0         | 0         | 30    | -   |
| disk.inode_used_pct | capacity     | yes | +   | 90   | 80    | -      | -       | 300   | 900         | 3600       | error | max  | 0         | 0         | 30    | -   |
| host.psi_cpu        | gauge        | yes | +   | 50   | 20    | -      | -       | 120   | 300         | 900        | warn  | max  | 0         | 0         | 30    | -   |
| host.psi_io         | gauge        | yes | +   | 40   | 15    | -      | -       | 120   | 300         | 900        | warn  | max  | 0         | 0         | 30    | -   |
| net.err_rate        | counter_rate | yes | +   | 1    | 0.1   | -      | -       | 60    | 180         | 600        | warn  | max  | 0         | 0         | 30    | -   |
| wifi.rssi           | gauge        | no  | -   | -80  | -75   | -      | -       | 120   | 300         | 900        | warn  | min  | 0         | 0         | 30    | -   |
| `*` (fallback)      | gauge        | no  | +   | -    | -     | 4      | 3       | 60    | 180         | 600        | warn  | max  | 0         | 0         | 30    | -   |

Notes on specific rules:

* **`host.temp`** derives its thresholds from `THROTTLE_TEMP_C` (default 80 °C):
  `trip = THROTTLE_TEMP_C − 5`, `clear = THROTTLE_TEMP_C − 10`. Move `SMOKEMON_THROTTLE_TEMP`
  for a different SoC and both move together.
* **`ping.rtt_med`** is the only `dynamic="rtt"` rule. Its thresholds come from the persisted
  per-node baseline rather than a fixed number: `trip = max(centre × 3, centre + 30)`,
  `clear = centre × 1.5`. Until the baseline has `min_baseline_n` (30) samples,
  `_thresholds()` returns `(None, None)` and only the z test can fire — and z is also gated on
  the same readiness, so **this rule cannot trip at all during warmup**. That is intentional:
  a latency rule with no idea what normal looks like has nothing to say.
* **`wifi.rssi`** has fixed thresholds but is marked `absolute=False`, so it takes the `expire`
  path (baseline thawed) rather than `persistent` if it outlives `INCIDENT_MAX_OPEN_S`.
  Harmless in practice — thawing the baseline does not move a fixed `trip` — but it means a
  24-hour-long weak-signal condition is reported as expired rather than persistent.

### Overrides

Sparse, per-field, so an operator never restates a whole rule to move one number:

```sh
SMOKEMON_RULES='ping.loss:trip=15,for_s=30;host.temp:trip=75'
```

Overridable fields: `trip`, `clear`, `trip_z`, `clear_z`, `for_s`, `clear_for_s`, `cooldown_s`,
`abs_floor`, `rel_floor`, `min_baseline_n` (numeric), and `severity`, `direction`, `peak_mode`
(string). A clause naming a signal with no rule creates one from the defaults. A malformed
clause is **skipped, not raised** — a typo in an env var must not stop a node monitoring.

Every incident row stores a `rule_hash` (sha1 of the effective rule after overrides, 12 hex
chars). An incident from last month is not interpretable after a threshold change unless the
row says which thresholds it was evaluated under.

---

## The baseline

Per-node learned estimate of what normal looks like on *this* box. A fleet-wide threshold
cannot know that one node sits behind a 4G modem where 90 ms is fine and another is on fibre
where 90 ms is a fault.

**Estimator:** EWMA of the centre, plus an EWMA of the absolute deviation as an online MAD
surrogate. Not a decaying-window median — that requires keeping the window, which is exactly
the memory and disk cost this pivot exists to remove.

### dt-derived alpha

```
a = 1 − exp(−dt / tau)          tau = BASELINE_TAU_S (86400 s)
```

`dt` is the actual wall-clock gap since the last update, so a signal fed every 10 s and one fed
every 300 s decay at the same real-world rate. Deriving `a` from a sample *count* instead would
make fast signals learn 30× faster than slow ones, and the central claim of the pivot — that
sampling rate stops being the important thing — would quietly stop holding right here.

### The MAD floor

```
scale = max(dev × 0.7979 × 1.4826,  abs_floor,  rel_floor × |centre|)
z     = (value − centre) / scale
```

`0.7979` converts `E|x−μ|` to MAD for a normal distribution; `1.4826` scales MAD to a
standard-deviation equivalent.

The floor exists because `dev` tends to 0 on a stable signal, and without it 0.1 ms of jitter
reads as z = 200. `ping.rtt_med` sets `abs_floor=2.0` (ms) and `rel_floor=0.05` (5% of the
centre), so both a fast fibre link and a slow modem get a sane denominator.

If `scale` is still below 1e-9 — a constant signal with no configured floor, typically a ratio
pinned at 0.0 — the spread is treated as unmeasurable rather than dividing by zero: identical to
the centre reads as 0, anything else saturates at ±50. Saturating rather than returning `inf`
keeps the value storable and comparable.

### The three poison defences

A baseline that learns during an incident teaches the node that the fault is normal. Three
independent mechanisms prevent that:

1. **Update only in `OK`.** `detect.evaluate` calls `baseline.update()` in exactly one branch:
   `OK` and not breaching. `ARMED`, `OPEN`, `CLOSING` and `COOLDOWN` all freeze it. `COOLDOWN`
   freezing matters as much as `OPEN` — the recovery tail is not representative either.
   (`baseline.py` itself refuses nothing; the freeze policy lives in one place, in `detect`.)
2. **Outlier gating (winsorising).** A sample beyond `BASELINE_GATE_Z` (4.0) still updates, but
   with its weight divided by `|z|`. A single spike that never persists long enough to arm an
   incident does not drag the centre; a genuine sustained shift still gets there, because each
   of its samples is gated less as the centre moves toward it.
3. **Alpha clamping.** `_alpha` clamps to `min(1.0, …)` and returns 1.0 for a non-positive `dt`.
   A `dt` of hours — node asleep, or a clock step — would otherwise give `a ≈ 1.0` and throw
   away everything learned so far in a single sample.

### Flush interval and the restart-gap trade-off

Baselines are flushed to `signal_baseline` at most every `BASELINE_FLUSH_S` (900 s), never per
sample. Per-sample would be roughly 8 600 extra commits a day on a node whose entire purpose is
to stop writing constantly, and would spend the whole SD-write budget on bookkeeping.

The accepted cost: **a crash loses up to one flush interval of learning.** `n` can go backwards
and warmup can lengthen after a restart. An EWMA with a one-day tau does not notice 15 minutes
of missed samples; the warmup counter (`min_baseline_n`) is the part that actually regresses,
and on a signal that was already warm it stays warm.

`baseline.thaw()` is the deliberate forget. It is called from exactly one place — `sweep()`, on
an expired **relative** rule — so the signal relearns the current regime.

---

## Reopen policy vs cooldown

Two different knobs answering two different questions. Conflating them is the most likely
future mistake here.

| | `cooldown_s` (per rule) | `INCIDENT_REOPEN_WINDOW_S` (global, 900 s) |
|---|---|---|
| question | when may the detector believe a re-trip without re-debouncing? | does a re-trip count as the *same occurrence*? |
| lives in | `detect.py`, the state machine | `incidents.py`, next to the persisted `closed_wall` |
| effect | governs the `COOLDOWN → OK` timer | governs `uid` reuse: same `uid` + `reopen` transition, or a new `uid` + `open` |

**What `cooldown_s` actually does.** Note carefully: it does **not** suppress a re-trip. A
breach while in `COOLDOWN` opens immediately, with no `ARMED` phase — the condition already
proved it persists, so it is believed at once and the onset is that very sample. What
`cooldown_s` governs is how long the machine waits before returning to `OK`, and therefore
(a) how long the baseline stays frozen through the recovery tail, and (b) how long a re-trip
gets to skip the `for_s` debounce.

**What the reopen window does.** Reusing a `uid` forever would fold a morning outage and an
evening outage into one semantically enormous incident. Minting a new `uid` on every re-trip
would turn a flapping link into a dozen incidents. The window separates the two cases:

```
same uid (reopen)   ⟺   same (signal, entity, rule)  AND  (now − closed_wall) ≤ 900 s
```

`uid` is also the alert key (`hubapi.open_incident_alerts`) — it is stable for the life of one
occurrence and changes when a genuinely new occurrence begins, which is exactly what a
re-notify cooldown needs.

---

## Expiry: absolute vs relative rules

An incident open longer than `INCIDENT_MAX_OPEN_S` (86 400 s) is force-closed. What happens next
depends on `Rule.absolute`:

**Relative rule** → `expired` transition, and `baseline.thaw()`. The condition has become the
new regime; relearn it.

**Absolute rule** → `persistent` transition, and the baseline stays **frozen**.

The distinction is invariant 9, and it is the single most important safety property in this
module. An absolute rule is a safety threshold, not an observation about what is typical. A disk
at 96% is still bad after 24 hours; a CPU at 90 °C is still bad after 24 hours. Thawing the
baseline there would train the node into accepting a permanent fault as normal, and letting
expiry emit a `close` would make it a **silent auto-acknowledge**: the incident would vanish
from the hub's open list while the fault continued.

So the run is closed (it must not sit open forever, or `stale` and genuine faults become
indistinguishable) but the record keeps saying `persistent`, and the rule keeps tripping.

---

## The decimation ladder

Evidence per incident, by phase:

| phase | source | count |
|---|---|---|
| `pre` | `ring.before(onset_mono, …)` — samples strictly older than the moment the breach **started** | ≤ `INCIDENT_PRE_SAMPLES` = 6 |
| `during` | the onset head at native cadence, then an exponential ladder | ≤ `INCIDENT_DURING_MAX` = 24 |
| `post` | `ring.tail(…)` at close | ≤ `INCIDENT_POST_SAMPLES` = 3 |

The `pre` cutoff is the moment the breach *started*, not the moment it was confirmed. The
samples in between are already anomalous, and labelling them baseline would put the anomaly
inside its own reference window — wrong to read and wrong to plot. Those go in as the `during`
head instead.

**The ladder.** The first `INCIDENT_DURING_HEAD` (6) samples are kept at native cadence, so the
shape immediately after the trigger is intact. After that, a sample is kept only once
`next_step` seconds have passed since the last kept one, and `next_step` then grows by
`INCIDENT_DURING_GROWTH` (2.0×) up to `INCIDENT_DURING_STEP_MAX` (3600 s):

```
head:   6 samples at native cadence
then:  +60s  +120s  +240s  +480s  +960s  +1920s  +3600s  +3600s  …  (capped)
```

Cumulative coverage from onset, at the defaults:

```
sample  6 →     60 s        sample 12 →   7 380 s  (2h03m)
sample  8 →    420 s        sample 18 →  28 980 s  (8h03m)
sample 10 →  1 860 s        sample 23 →  46 980 s (13h03m)  ← ladder exhausted
```

**Worst case per incident: 6 + 24 + 3 = 33 sample rows**, regardless of duration. A three-day
outage costs about the same as a three-minute one. Beyond sample 24 no more `during` samples are
kept, so an incident longer than ~13 hours has no evidence covering its final stretch — the
`worst_value` column is what carries that period, which is why it is tracked over every sample
and re-persisted on every one, kept or not.

Plus transition rows: 2 in the simple case (`open` + a terminal), more with reopens.

**Storm budget.** Beyond `INCIDENT_MAX_OPEN` (16) concurrent incidents, a new incident records
its transitions only and captures no samples. Degrade detail, never detection — a node in a
storm is exactly when we must not stop noticing things.

---

## Restart semantics

`incidents.load_open()` reads `incident_state` for rows in `('open','closing','cooldown')` and
calls `detect.restore()` for each. Per state:

| persisted state | restored as | notes |
|---|---|---|
| `OK` | not persisted | nothing to restore |
| `ARMED` | **never persisted, never restored** | an unconfirmed anomaly does not survive a restart; its debounce starts over. The intended cost of refusing to write candidates to disk (invariant 2). |
| `OPEN` | `OPEN` | the incident **resumes** — the hub sees one incident spanning the restart, not two |
| `CLOSING` | `OPEN`, hold timer reset | fail toward a longer incident, never toward a close we did not actually observe |
| `COOLDOWN` | `COOLDOWN` | timer re-seeded from wall clock |

Monotonic timers have no meaning across processes, so each is re-seeded:
`since_mono = now_mono − (now_wall − changed_wall)`. If that comes out negative — the clock
stepped backwards while we were down — the timer is treated as freshly started. Fail toward a
longer debounce, never toward a spurious incident.

Resuming rather than reopening is why `incident_state` is a table and not an in-memory set. A
still-broken condition must continue its incident; `events.py`'s in-memory `_active` set cannot
do that, which is precisely why `detect.py` is a separate path rather than an extension of it.

---

## Environment variables

Every `SMOKEMON_*` var the detector path reads, with its default. Authority is `config.py`.

### Signal registry

| var | default | meaning |
|---|---|---|
| `SMOKEMON_SIGNAL_MAX` | `48` | max distinct `(signal, entity)` pairs held in memory |
| `SMOKEMON_SIGNAL_RING` | `64` | samples retained per signal |
| `SMOKEMON_SIGNAL_STALE_S` | `600` | a signal silent this long with an incident open closes it as `stale` |

### Baseline

| var | default | meaning |
|---|---|---|
| `SMOKEMON_BASELINE_TAU_S` | `86400` | EWMA time constant, wall-clock seconds |
| `SMOKEMON_BASELINE_GATE_Z` | `4.0` | winsorising threshold; beyond this a sample's weight is divided by `\|z\|` |
| `SMOKEMON_BASELINE_MAX_N` | `100000` | saturation point of the warmup counter |
| `SMOKEMON_BASELINE_FLUSH_S` | `900` | max interval between disk flushes; also the worst-case learning lost to a crash |

### Rules

| var | default | meaning |
|---|---|---|
| `SMOKEMON_RULES` | `""` | sparse per-field rule overrides (see above) |
| `SMOKEMON_THROTTLE_TEMP` | `80` | °C at which the SoC throttles; `host.temp`'s trip/clear derive from it |

### Incidents and evidence

| var | default | meaning |
|---|---|---|
| `SMOKEMON_INCIDENT_PRE` | `6` | `pre`-phase samples captured at trip time |
| `SMOKEMON_INCIDENT_POST` | `3` | `post`-phase samples captured at close |
| `SMOKEMON_INCIDENT_DURING_MAX` | `24` | hard cap on `during` samples per incident |
| `SMOKEMON_INCIDENT_DURING_HEAD` | `6` | leading `during` samples kept at native cadence |
| `SMOKEMON_INCIDENT_DURING_STEP0` | `60` | first ladder step, seconds |
| `SMOKEMON_INCIDENT_DURING_GROWTH` | `2.0` | ladder step multiplier |
| `SMOKEMON_INCIDENT_DURING_STEP_MAX` | `3600` | ladder step ceiling, seconds |
| `SMOKEMON_INCIDENT_MAX_OPEN` | `16` | beyond this many concurrent incidents, new ones record transitions only |
| `SMOKEMON_INCIDENT_MAX_OPEN_S` | `86400` | force-close an incident open this long (`expired` or `persistent`) |
| `SMOKEMON_INCIDENT_REOPEN_WINDOW_S` | `900` | re-trip within this of the close continues the same `uid` |

### Operation

| var | default | meaning |
|---|---|---|
| `SMOKEMON_DETECT_DRYRUN` | `0` | run the full detector but log what it *would* have written instead of writing it |
| `SMOKEMON_HEARTBEAT_INTERVAL` | `300` | the only row a healthy node writes; also what the hub derives liveness from |
| `SMOKEMON_NODE` | hostname | the node half of `incident_key` = `NODE/signal/entity` |

`SMOKEMON_DETECT_DRYRUN=1` is the bring-up aid: run a node for a day in that mode to see the
real incident rate before committing to thresholds, and to test a threshold change before
releasing it to the fleet. Nothing reaches disk, so it is safe to leave on.

---

## Write budget

Measured, not estimated — `scripts/bench_write_budget.py` feeds a simulated day through the
real detector and the real persistence path and reports what reached disk.

A day of steady state (10 s ping, 30 s host, 300 s heartbeat, two three-minute incidents):

| | measured |
|---|---|
| commits | 326 / day |
| appended to db + WAL | 2.9 MB / day |
| final database size | 0.13 MB |
| rows | 288 heartbeats, 4 incident transitions, 32 incident samples, 4 events |

`device_facts` and `log_excerpts` stay empty in steady state by design: the first is
delta-coded and writes only when a fact changes, the second only on an incident with evidence
capture enabled.

Two numbers are reported because they answer different questions. **Appended bytes** is the
summed growth of the database and its WAL — portable, and a good proxy in WAL mode because the
WAL is append-only. **Physical writes** comes from `/proc/self/io` `write_bytes`, the kernel's
count of bytes handed to the storage layer, which is what actually wears an SD card. The gap
between them is write amplification: SQLite commits whole pages and appends a commit frame per
transaction, so a ~180-byte heartbeat row does not cost 180 bytes.

Physical writes require Linux. Run the benchmark on the node to get the figure that matters for
card wear; a developer machine reports the portable number only.

The commit count is the number to watch. Bytes follow from it, because at this row size the
per-commit overhead dominates the payload — which is also why `BASELINE_FLUSH_S` exists at all:
persisting the baseline per sample would add ~8600 commits a day to a budget of 326.
