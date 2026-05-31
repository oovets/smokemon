# smokemon — quickstart

the copy-paste version. full options and deploy details live in [INSTALL.md](INSTALL.md);
what every metric means is in [README.md](README.md).

## install

### linux — one line

installs deps, runs the collectors as systemd services, and adds a `smoke` command.

```bash
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --node "$(hostname)"
```

open a new shell so `smoke` is on your PATH. that's it — local only, nothing is shipped anywhere.

### macOS — manual

```bash
git clone https://github.com/oovets/smokemon.git ~/smokemon && cd ~/smokemon
brew install fping mtr iperf3
python3 -m pip install --user plotext

# start collecting (one process, all probes):
PYTHONPATH=. python3 -m smokemon.collect all &
```

add a `smoke` shortcut for this shell (or see [INSTALL.md](INSTALL.md) for always-on launchd services):

```bash
alias smoke='PYTHONPATH=~/smokemon python3 -m smokemon.cli'
```

## use

```bash
smoke              # static dashboard, last 6h
smoke live 24h     # live, redraws in place
smoke kiosk 24h    # live + clean, for a wall display
smoke status       # one-line health summary
smoke incidents    # detected problems + likely cause
smoke digest       # plain-english summary of the window
smoke png          # high-res PNG (needs matplotlib; macOS/hub)
```

common flags: `--hours N` / `--minutes N`, `--panels ping,net,wifi,host,…`, `--targets a,b`.

## what it looks like

the text surfaces are pure stdlib, so they run anywhere. example output (illustrative):

```console
$ smoke status
internet ▁▂▁▁▃▁ 4ms 0% · wifi ▆▆▅▆ -52dBm · cpu ▁▁▂▁ 45°C · healthy

$ smoke incidents --hours 24
14:32-14:35  latency-spike  +410%   blame: cpu 98% · new proc "backup" · temp 71°C
03:10-03:11  packet-loss     18%    blame: wifi roam (2 bssids) · rssi -74dBm

$ smoke digest --hours 24
uptime 99.8% (2 incidents, 3m hard-down). peak 210ms @14:33 (cpu-correlated).
bufferbloat B. 2 wifi roams. cpu max 71°C (9°C from throttle). disk full ~14d.
```

the graphical surfaces — `smoke png`, `smoke tui`, and the hub dashboard at `GET /` — draw
the same data as panels stacked on one timeline.

## multi-node + central hub

one hub aggregates many nodes (push model).

```bash
# hub (once):
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --hub --secret MY_SECRET

# each node (the secret must match the hub):
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --node NAME --hub-url http://HUB-HOST:8765/ingest --secret MY_SECRET
```

```bash
smoke fleet --hub-url http://HUB-HOST:8765            # worst-first status, one line/node
smoke fleet --hub-url http://HUB-HOST:8765 live       # repaints in place
smoke fleet --hub-url http://HUB-HOST:8765 --heatmap  # node × hour loss/rtt grid
```

```bash
smoke tui --db /opt/smokemon/data/smokemon-hub.db --node NAME
```

repoint a node to a different hub later (writes `SMOKEMON_HUB_URL`, applied on the shipper's next run, <=15s):

```bash
smoke hub NEW-HUB-HOST
```

---

every flag and service → [INSTALL.md](INSTALL.md) · what it measures → [README.md](README.md)
