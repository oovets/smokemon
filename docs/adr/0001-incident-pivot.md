# 0001 — Incident-centric storage

Status: accepted
Date: 2026-07

---

## Context

Smokemon stored continuous time series. Every probe wrote every sample: `ping_rtts` alone
produced roughly 345 000 rows a day per node, and all of it was shipped to the hub, where
anomaly detection ran after the fact as SQL over the accumulated history.

The cost was not primarily storage. It was four things at once:

* **Write budget.** The nodes are Raspberry Pis and Jetsons on SD cards and eMMC. A monitoring
  agent that writes continuously spends a finite flash-write budget on recording that nothing
  happened.
* **Ship volume.** Raw per-ping rows were about 85% of wire traffic, and the hub had no reader
  for them; they had already been reduced to percentiles before anything looked at them.
* **Hub-side detection.** Anomaly rules ran as SQL over a window on the hub. A window median
  used as a baseline is poisonable by an incident sitting inside that same window, and every
  rule's sensitivity silently depended on the sampling rate of whatever probe produced the data.
* **Rollups.** `_1m`/`_1h` downsampling existed only to make long windows of dense data cheap
  to query — infrastructure whose entire purpose was to manage a problem we had created.

More importantly it conflicted with the project's own stated design principles. Smokemon's
premise is that the observer must cost less than what it explains, and that detail should
degrade gracefully rather than the footprint overrunning target. Storing normal operation
forever is the opposite: it is noise, retained at full fidelity, at permanent cost, on the
assumption that some future question might want it.

## Decision

Change what the system considers valuable information.

> Observe continuously. Persist selectively. Ship evidence. Forget noise.

Normal operation becomes ephemeral. Deviation becomes durable. The **incident** becomes the
primary data model.

Concretely:

* Probes sample and hand values to a detector; they no longer decide anything and no longer
  write sample rows.
* Normal operation lives in a bounded in-memory ring (`signals.py`) and is never written.
* Only confirmed incident state transitions reach disk, plus a decimated evidence window
  captured around each one.
* A healthy node's only periodic disk write is the heartbeat.
* Detection moves to the node, online, in `detect.py`. The hub reduces and reports; it no
  longer decides.
* `rollup.py` and its tables are deleted — without dense data there is nothing to roll up.

The operational reference for the detector — rule table, state machine, baseline, env vars —
is [`docs/detector-spec.md`](../detector-spec.md).

## Five load-bearing design decisions

These are the ones that, if reversed, break the system rather than merely change it.

### D1 — `incidents` is an append-only transition log

Forced by the shipper. `ship.gather()` walks each table with a strict monotonic rowid cursor
and the hub inserts with `INSERT OR IGNORE`. An `UPDATE` to an already-shipped row changes no
rowid, so **the hub would never see it** — the incident would close on the node and stay open
on the hub forever.

So the lifecycle is expressed as separate rows sharing a `uid`
(`open|reopen|close|stale|expired|persistent`), and the hub reduces per `(node, uid)`. There is
no "current row" to select; every read does the reduction.

The constraint and the right design happen to agree. An append-only log also buys replay,
idempotence, readable history, and correct handling of late arrival for free.

### D2 — `uid` is a content key, not a foreign key

Child rows (`incident_samples`, `log_excerpts`) key on the node-generated
`uid TEXT`, never on a local rowid. `hub._insert_std` remaps ids for one legacy table only; a
rowid foreign key would be meaningless on the hub and would recreate the `ping_rtts` bug where
redelivered children were silently dropped.

Because `uid` is derived from content (`sha1(key|opened_wall)`), a sample that arrives before
its parent transition is an unjoined-but-valid row that completes when the parent lands. The
loaders are written to exploit this: `query.load_incident_samples` queries
`incident_samples` **alone** and never joins, because a join would hide exactly the rows that
prove what happened. `hubapi.hub_health` promotes the orphan count to a first-class metric —
a steady trickle is normal, a growing backlog of old orphans means transition rows are being
lost and every incident view is quietly incomplete.

### D3 — node-local mutable state is kept out of `schema._BODY`

Membership in `_BODY` *is* the ship switch. Anything mutable — `incident_state`,
`signal_baseline`, `log_cursors` — is declared by its owning module instead, so the shipper
never sees it. This follows the `log_cursors` precedent and is what makes D1 tenable: the
append-only log is what ships, the mutable working state stays home.

### D4 — monotonic clock for logic, wall clock for storage

A hard boundary in the implementation. Every duration the detector reasons about — debounce,
hysteresis hold, cooldown, staleness — is measured on `time.monotonic()`. Wall clock is carried
alongside purely so stored rows have a timestamp a human can read.

Pi and Jetson nodes NTP-step at boot, routinely by hours, and a node with a dead RTC boots to
1970. That is the normal state, not the exception. A debounce measured on wall clock would
either fire on the first sample or never. The one place wall clock enters the logic is
`INCIDENT_MAX_OPEN_S` in `sweep()`, which compares against the persisted `opened_wall` because
it must survive a restart; `detect.restore` re-seeds monotonic timers from wall clock and
treats negative arithmetic (the clock stepped backwards while we were down) as a freshly
started timer — failing toward a longer debounce, never toward a spurious incident.

### D5 — `detect.evaluate` returns declarative actions

`detect.py` never touches SQLite. `evaluate()` returns a list of `Action` namedtuples
describing what should be persisted; `incidents.py` decides how, and owns the transaction
boundary.

Policy code — thresholds, debounce, hysteresis, reopen semantics — is the single largest
future complexity risk in this system. Keeping it in a pure function makes the entire state
machine testable against synthetic sample sequences with no database at all, which is the only
way it stays honest as rules accumulate.

The split also has a second edge: `detect` never decides incident *identity*. Whether an `open`
continues an existing `uid` or mints a new one is the reopen policy, which lives in
`incidents.py` next to the persisted `closed_wall` it needs.

## Consequences

**What this buys.** A healthy node writes one heartbeat row per interval and nothing else. Ship
volume drops by the entire weight of normal operation. Detection is per-node and rate-independent:
a rule expressed in seconds behaves identically whatever cadence the probe samples at. The
baseline is per-node, so "90 ms is fine behind this 4G modem and a fault on that fibre link" is
expressible for the first time. Rollups, and the aggregation bugs that came with them, are gone.

**What you can no longer do — and this is the real cost.**

*You cannot retro-analyse what you had no rule for.* If a signal degrades in a way no rule
describes, there is no stored data to go back to. Previously you could ask a new question of
old data; now you can only ask questions you already knew to ask. Adding a rule starts
collecting evidence from that moment forward and never backward.

This is a deliberate trade, not an oversight. The retained history was overwhelmingly not used
for retrospective discovery — it was used to answer "what happened during this incident", which
the evidence window answers better and for 0.01% of the rows. But the capability is genuinely
gone, and the mitigations are partial:

* `SMOKEMON_DETECT_DRYRUN=1` runs the full detector and logs what it *would* have written.
  Run a node in that mode for a day to see the real incident rate before committing thresholds.
* Every incident row stores `threshold`/`baseline`/`baseline_mad`/`z` **as evaluated**, plus a
  `rule_hash`, so an old incident stays interpretable after a rule change. The raw data that
  would otherwise let you re-derive them no longer exists, so it has to be stamped at the time.

**Other consequences.**

* Each incident costs a bounded number of rows regardless of duration. A three-day outage and a
  three-minute one cost about the same. This is a feature, but it means a long incident's middle
  is sparse by construction.
* A crash loses up to `BASELINE_FLUSH_S` of learning. `n` can go backwards and warmup can
  lengthen after a restart.
* ARMED is never persisted, so an unconfirmed anomaly does not survive a restart and its
  debounce starts over. That is the intended price of refusing to write candidates to disk.
* The hub can no longer draw a chart of normal operation, because normal operation is not
  recorded anywhere. `hubapi.prometheus` deliberately exports no per-signal gauges: synthesising
  a series from incident windows would export a chart made only of the bad moments — worse than
  exporting nothing, because it would look complete.
* Old per-probe tables must be actively pruned or dropped; they do not disappear on upgrade.

## Invariants

The contract. Each has at least one test.

1. Normal operation never writes signal samples to disk.
2. ARMED never survives a restart.
3. A confirmed incident always gets an append-only `open` transition.
4. A transition and its node-local state change happen in the **same SQLite transaction**.
5. All distributed relations use `uid`, never local rowids.
6. All shipping goes through the same cursor and retry mechanism.
7. Evidence may fail without the incident failing.
8. The baseline updates only in OK.
9. **Absolute safety rules are never trained away by expiry.**
10. Memory use is hard-bounded by `SIGNAL_MAX` × `SIGNAL_RING`.
11. Every incident has a maximum number of persisted sample rows.
12. The hub never reads the absence of a `close` as evidence the fault persists.
13. Old tables must be actively pruned or dropped.
14. The same shipment may safely be delivered more than once.
15. Late arrival never changes correctness.
