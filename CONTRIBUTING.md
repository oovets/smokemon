# Contributing to smokemon

Thanks for taking the time. smokemon is small, opinionated, and meant to stay stdlib-on-the-node. Patches and issues are welcome; please skim the points below first so we agree on the shape of changes.

## Ground rules

- **Node-side code stays stdlib-only.** Collectors, ship, and hub run on Raspberry Pi / Jetson / Debian boxes that should not need `pip install` to function. `plotext` (TUI) and `matplotlib`+`numpy` (PNG) are explicitly extras and never imported from the daemons.
- **Never break working functionality.** Schema changes must be additive (use `ensure_body_columns` migrations). Wire-format changes between node and hub must be backward compatible - version the payload, do not remove fields.
- **No fabricated or sample data.** All metrics must come from real `/proc`, `/sys`, or subprocess sources. No placeholder values.
- **No emoji in code, commits, or docs.** Project style.

## Getting set up

```bash
git clone https://github.com/oovets/smokemon.git
cd smokemon
python3 -m pip install -e ".[tui,png,dev]"      # editable + all extras
python3 -m pytest                               # run the smoke tests
ruff check .                                    # lint
```

See [INSTALL.md](INSTALL.md) for the full installation reference (launchd, systemd, hub deployment).

## Filing issues

- **Bug reports** should include: OS + version, Python version, `smokemon` git SHA or version, the exact command that failed, and the relevant lines from `journalctl -u smokemon-*` (Linux) or `~/smokemon/logs/*.err.log` (macOS).
- **Feature requests** should describe the metric or visualisation you want, and (if a metric) the data source you propose (`/proc/...` file, `/sys/...` glob, or subprocess + its rough cost in ms).

The issue templates in `.github/ISSUE_TEMPLATE/` walk through both.

## Pull requests

- Keep PRs focused. One feature or one fix per PR.
- Run `ruff check .` and `python3 -m pytest` before pushing.
- Add a CHANGELOG.md entry under `## [Unreleased]`. Follow the existing Keep-a-Changelog structure.
- For new metrics: extend `_BODY` in `smokemon/schema.py`, then `ensure_body_columns` makes the migration automatic. Add a loader to `smokemon/query.py` and a panel-build in both renderers (`smokemon/render/png.py` and `smokemon/render/tui.py`).
- For new probes: add a module under `smokemon/probes/` and wire it into `smokemon/collect.py`. Tier it (fast=10s, slow=60s, or your own slow interval) based on cost.

## Coding style

- Python 3.10+, type hints where they pay rent.
- `ruff` config lives in `pyproject.toml`; the rules are intentionally light.
- Comments should explain *why* and *what is non-obvious*, not narrate the code.
- Functions over classes when there is no state to bundle.

## Tests

Tests live in `tests/`. They run against a temp SQLite DB and exercise the migration, query, ingest, and adapter paths. Linux-only metrics are tested via monkey-patching so the suite runs on macOS too.

```bash
python3 -m pytest -v
```

## Security issues

Do not file public issues for security problems. See [SECURITY.md](SECURITY.md).
