# contributing

smokemon is small, opinionated, and meant to stay stdlib-on-the-node. patches and issues
are welcome — please skim the points below first so we agree on the shape of changes.

security issues -> [SECURITY.md](SECURITY.md) (never a public issue)
install reference -> [INSTALL.md](INSTALL.md)

```
== ground rules ==

- everything stays stdlib-only. collectors, ship, hub and cli all run on raspberry pi /
  jetson / debian boxes that should not need pip install to function. there are no optional
  extras and no renderer dependencies; the only non-stdlib things anywhere are fping and iw.

- nothing is written while things are normal. normal operation lives in the bounded
  in-memory ring (smokemon/signals.py); only confirmed incident state transitions and the
  evidence window around them reach disk. a change that reintroduces a continuous series is
  the one change this project will not take — see docs/adr/0001-incident-pivot.md.

- probes sample, detect.py decides. a probe never applies a threshold, a debounce or a
  hysteresis band of its own; it feeds (signal, entity, value) and stops there.

- node footprint is an absolute product constraint. never add node-side probes or
  integrations that materially increase rss/cpu/io/network use. prefer /proc, /sys,
  short bounded socket reads, and explicit allowlists; avoid log streaming, docker log
  scans, broad service discovery, large scrape bodies, and always-on subprocess tails.

- never break working functionality. schema changes must be additive (use
  ensure_body_columns migrations). node<->hub wire-format changes stay backward compatible
  - version the payload, do not remove fields.

- no fabricated or sample data. all metrics come from real /proc, /sys, or subprocess
  sources. no placeholder values.

- no emoji in code, commits, or docs. project style.
```

```bash
# getting set up
git clone https://github.com/oovets/smokemon.git
cd smokemon
python3 -m pip install -e ".[dev]"              # editable + pytest/ruff
python3 -m pytest                               # run the smoke tests
ruff check .                                    # lint
```

```
== filing issues ==

- bug reports: os + version, python version, smokemon git sha or version, the exact
  command that failed, and the relevant lines from journalctl -u smokemon-*.

- feature requests: the signal or view you want, and (if a signal) the data source you
  propose (/proc/... file, /sys/... glob, or subprocess + its rough cost in ms) plus the
  rule that should make it an incident.
the issue templates in .github/ISSUE_TEMPLATE/ walk through both.
```

```
== pull requests ==

- keep prs focused. one feature or one fix per pr.

- run ruff check . and python3 -m pytest before pushing.

- add a changelog.md entry under the == unreleased == block, matching the house style
  (lowercase, section headers inside the fenced code boxes).

- new signals: give it a kind in detect.SIGNAL_KINDS and, if the generic z fallback is not
  right for it, a rule in detect.RULES. document it in docs/detector-spec.md. new columns go
  in _BODY in smokemon/schema.py, where ensure_body_columns makes the migration automatic.

- new probes: add a module under smokemon/probes/ and wire it into smokemon/collect.py.
  tier it (fast=10s, slow=60s, or your own slow interval) based on cost. it must feed the
  detector, not write a series of its own.
```

```
== coding style ==

- python 3.10+, type hints where they pay rent.

- ruff config lives in pyproject.toml; the rules are intentionally light.

- comments explain why and what is non-obvious, not narrate the code.

- functions over classes when there is no state to bundle.
```

```
== tests ==

tests live in tests/. they run against a temp sqlite db and exercise the migration, query,
ingest, and adapter paths. metrics that depend on specific kernel interfaces are tested via
monkey-patching, so the suite runs without them. run: python3 -m pytest -v
```
