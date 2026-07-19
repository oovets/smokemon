"""Single entry point, so the whole agent can ship as one file.

    smokemon.pyz              -> the CLI (smoke)
    smokemon.pyz collect      -> the node daemon
    smokemon.pyz hub          -> the hub
    smokemon.pyz ship         -> one manual drain
    smokemon.pyz prune        -> one manual retention sweep

The package is stdlib-only, which means `python3 -m zipapp` turns it into a single executable
archive. That is the whole reason this file exists: a deploy becomes "copy one file", with no
git checkout on the node, no clone, and no `git pull --ff-only` to fail on a rewritten history.
Version identity is the file itself.

The daemons stay importable as modules too (`python -m smokemon.collect`), so a checkout-based
install keeps working.
"""

import sys

_DAEMONS = {
    "collect": ("smokemon.collect", "main"),
    "hub": ("smokemon.hub", "main"),
    "ship": ("smokemon.ship", "main"),
    "prune": ("smokemon.prune", "main"),
    "notify": ("smokemon.notify", "main"),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in _DAEMONS:
        mod_name, fn = _DAEMONS[argv[0]]
        # Imported lazily: the CLI should not pay for the hub's http.server, and the collector
        # should not import the query layer.
        mod = __import__(mod_name, fromlist=[fn])
        sys.argv = [f"smokemon {argv[0]}", *argv[1:]]
        return getattr(mod, fn)()
    from smokemon.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
