"""WiFi signal (RSSI/noise/tx/channel) via the OS adapter. No-op if not on WiFi."""

import time

from .. import adapters, config, schema


def collect(conn) -> None:
    if not config.WIFI_ENABLED:
        return
    w = adapters.wifi_probe()
    if w:
        schema.insert(conn, "wifi_samples", [{"ts": time.time(), **w}])
        conn.commit()
