# contributing

smokemon is small, opinionated, and meant to stay stdlib-on-the-node. patches and issues
are welcome — please skim the points below first so we agree on the shape of changes.

security issues -> [SECURITY.md](SECURITY.md) (never a public issue)
install reference -> [INSTALL.md](INSTALL.md)

```
== ground rules ==

- node-side code stays stdlib-only. collectors, ship, and hub run on raspberry pi /
  jetson / debian boxes that should not need pip install to function. plotext (tui) and
  matplotlib+numpy (png) are extras, never imported from the daemons.

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
python3 -m pip install -e ".[tui,png,dev]"      # editable + all extras
python3 -m pytest                               # run the smoke tests
ruff check .                                    # lint
```

```
== filing issues ==

- bug reports: os + version, python version, smokemon git sha or version, the exact
  command that failed, and the relevant lines from journalctl -u smokemon-* (linux) or
  ~/smokemon/logs/*.err.log (macos).

- feature requests: the metric or visualisation you want, and (if a metric) the data
  source you propose (/proc/... file, /sys/... glob, or subprocess + its rough cost in ms).
the issue templates in .github/ISSUE_TEMPLATE/ walk through both.
```

```
== pull requests ==

- keep prs focused. one feature or one fix per pr.

- run ruff check . and python3 -m pytest before pushing.

- add a changelog.md entry under the == unreleased == block, matching the house style
  (lowercase, section headers inside the fenced code boxes).

- new metrics: extend _BODY in smokemon/schema.py, then ensure_body_columns makes the
  migration automatic. add a loader to smokemon/query.py and a panel-build in both
  renderers (smokemon/render/png.py and smokemon/render/tui.py).

- new probes: add a module under smokemon/probes/ and wire it into smokemon/collect.py.
  tier it (fast=10s, slow=60s, or your own slow interval) based on cost.
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
ingest, and adapter paths. linux-only metrics are tested via monkey-patching so the suite
runs on macos too. run: python3 -m pytest -v
```
