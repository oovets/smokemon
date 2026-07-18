"""Every public node-side module must import cleanly with stdlib only."""

import importlib

import pytest

NODE_MODULES = [
    "smokemon.config",
    "smokemon.core",
    "smokemon.schema",
    "smokemon.query",
    "smokemon.ship",
    "smokemon.collect",
    "smokemon.signals",
    "smokemon.baseline",
    "smokemon.detect",
    "smokemon.incidents",
    "smokemon.heartbeat",
    "smokemon.adapters",
    "smokemon.adapters.linux",
    "smokemon.probes.ping",
    "smokemon.probes.net",
    "smokemon.probes.wifi",
    "smokemon.probes.host",
    "smokemon.probes.inventory",
    "smokemon.probes.logexcerpt",
]


@pytest.mark.parametrize("mod", NODE_MODULES)
def test_node_module_imports(mod):
    importlib.import_module(mod)


def test_cli_imports():
    """The CLI is the one entry point a node user runs; it must import on stdlib alone."""
    importlib.import_module("smokemon.cli")
