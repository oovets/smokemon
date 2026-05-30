"""Every public node-side module must import cleanly with stdlib only."""

import importlib

import pytest

NODE_MODULES = [
    "smokemon.config",
    "smokemon.core",
    "smokemon.schema",
    "smokemon.query",
    "smokemon.hub",
    "smokemon.ship",
    "smokemon.collect",
    "smokemon.adapters",
    "smokemon.adapters.linux",
    "smokemon.adapters.darwin",
    "smokemon.probes.ping",
    "smokemon.probes.net",
    "smokemon.probes.http",
    "smokemon.probes.mtr",
    "smokemon.probes.wifi",
    "smokemon.probes.iperf",
    "smokemon.probes.host",
    "smokemon.probes.ext",
    "smokemon.probes.synthetic",
    "smokemon.probes.redisq",
    "smokemon.probes.dockerps",
    "smokemon.probes.pipeline",
]


@pytest.mark.parametrize("mod", NODE_MODULES)
def test_node_module_imports(mod):
    importlib.import_module(mod)


def test_render_imports_optional():
    """Renderers can need plotext / matplotlib. The cli must degrade gracefully when
    those are missing - it does so by deferring the import to the subcommand."""
    importlib.import_module("smokemon.cli")
