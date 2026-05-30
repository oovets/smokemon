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

repoint a node to a different hub later (writes `SMOKEMON_HUB_URL`, applied within 60s):

```bash
smoke hub NEW-HUB-HOST
```

---

every flag and service → [INSTALL.md](INSTALL.md) · what it measures → [README.md](README.md)
