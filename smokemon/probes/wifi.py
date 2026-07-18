"""WiFi signal strength, fed to the detector. No-op if disabled or not on WiFi.

Samples RSSI via the OS adapter and hands it to incidents.evaluate() under a fixed (empty)
entity. Nothing is written per cycle: the detector holds samples in memory and persists only
what a rule confirms. The adapter also reports noise/tx-rate/channel, which are read as part
of the same call but are not evaluated -- no rule consumes them."""

import time

from .. import adapters, config, incidents


def collect(conn) -> None:
    if not config.WIFI_ENABLED:
        return
    w = adapters.wifi_probe()
    if not w:
        return
    ts = time.time()
    # RSSI is dBm and lower is worse, which the rule declares explicitly -- inheriting the
    # generic fallback here would open an incident every time reception improved. Entity is
    # empty: a node has one wireless link, and keying on SSID would mint a fresh cold
    # baseline every time it roamed between APs on the same network.
    incidents.evaluate(conn, "wifi.rssi", "", w.get("rssi_dbm"), ts)
