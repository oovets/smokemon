"""Seed a throwaway demo hub DB with ping + net + port traffic across a few nodes, for local
visual smoke-testing of the dashboard. Writes to $SMOKEMON_HUB_DB. Not shipped/imported anywhere."""
import os
import time

from smokemon import core, schema

db = os.environ["SMOKEMON_HUB_DB"]
if os.path.exists(db):
    os.remove(db)
conn = core.connect(db)
schema.init_hub(conn)
now = time.time()
nodes = ["cam-north", "cam-south", "jetson-lab", "vps-sthlm"]
for ni, node in enumerate(nodes):
    # ping: ~6h of 1-min samples, an outage on cam-south
    for i in range(360):
        ts = now - i * 60
        loss = 100.0 if (node == "cam-south" and 80 < i < 110) else (5.0 if i % 50 == 0 else 0.0)
        med = 8.0 + ni * 6 + (i % 9)
        schema.insert(conn, "ping_runs", [{"ts": ts, "target": "1.1.1.1", "sent": 20, "recv": 20,
                      "loss_pct": loss, "rtt_min": 5.0, "rtt_median": med, "rtt_max": med + 6}], node=node)
    # net: cumulative byte gauge rising at a node-specific rate
    cum_i = cum_o = 0
    for i in range(72):
        ts = now - i * 300
        cum_i += (ni + 1) * 20_000_000
        cum_o += (ni + 1) * 5_000_000
        schema.insert(conn, "net_samples", [{"ts": ts, "iface": "eth0", "ibytes": cum_i,
                      "obytes": cum_o, "ipkts": 0, "opkts": 0}], node=node)
    # ports: rtsp (8554), raw-video (5000), netdata (19999), https (443) cumulative byte gauges
    gauges = {8554: 0, 5000: 0, 19999: 0, 443: 0}
    rates = {8554: (ni + 1) * 8_000_000, 5000: (ni + 1) * 30_000_000, 19999: 200_000, 443: 1_000_000}
    for i in range(72):
        ts = now - i * 300
        rows = []
        for port, g in gauges.items():
            gauges[port] += rates[port]
            rows.append({"ts": ts, "proto": "tcp", "dir": "out" if port in (8554, 443) else "in",
                         "port": port, "conns": 2, "peers": 2, "listening": 0,
                         "bytes_sent": gauges[port], "bytes_recv": gauges[port] // 3})
        schema.insert(conn, "port_samples", rows, node=node)
conn.commit()
conn.close()
print("seeded", db, "with", len(nodes), "nodes")
