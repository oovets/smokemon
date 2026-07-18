# smokemon — quickstart

the copy-paste version. full options and deploy details live in [INSTALL.md](INSTALL.md);
what every signal means is in [README.md](README.md).

## install

### one line

installs deps, runs the collectors as systemd services, and adds a `smoke` command.

```bash
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --node "$(hostname)"
```

open a new shell so `smoke` is on your PATH. that's it — local only, nothing is shipped anywhere.

### manual — no systemd

```bash
git clone https://github.com/oovets/smokemon.git ~/smokemon && cd ~/smokemon
sudo apt install fping iw

# start collecting (one process, all probes):
PYTHONPATH=. python3 -m smokemon.collect all &
```

add a `smoke` shortcut for this shell (or see [INSTALL.md](INSTALL.md) for always-on systemd services):

```bash
alias smoke='PYTHONPATH=~/smokemon python3 -m smokemon.cli'
```

## use

```bash
smoke              # one-line health summary, last 6h
smoke incidents    # what broke, where, still broken?
smoke incident UID # one incident in full, with the evidence captured around it
smoke digest       # plain-english summary of the window
smoke footprint    # what the collectors actually cost
```

common flags: `--hours N` / `--minutes N`, `--node NAME`, `--db PATH`.

## what it looks like

everything is stdlib text, so it runs anywhere. example output (illustrative):

```console
$ smoke status
healthy · heartbeat 42s ago · cpu 6% · 51C

$ smoke incidents --hours 24
INCIDENTS — 2 in the last 24.0h · 1 ongoing · 1 closed

      opened       node      signal                         worst   duration  state
error 07-18 14:32 pi-hall   ping.loss/1.1.1.1                 34.5      3m12s  closed
warn  07-18 09:04 jetson-01 host.temp                           78     6h20m   ongoing

$ smoke incident a1b2c3d4e5f6
INCIDENT a1b2c3d4e5f6 — pi-hall — ping.loss/1.1.1.1
...
samples (12):
  pre        6  ▁▁▁▂▁▁  07-18 14:31 → 07-18 14:32
  during     3  ▆█▇      07-18 14:32 → 07-18 14:34
  post       3  ▁▁▁      07-18 14:35 → 07-18 14:36
```

a quiet node prints `(nothing recorded in this window)` — that is the normal, healthy state.
nothing is written while things are fine, so an empty incident feed is the goal, not a gap in
the data.

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
smoke fleet --hub-url http://HUB-HOST:8765            # one line/node, worst first
smoke fleet --hub-url http://HUB-HOST:8765 live       # repaints in place
smoke fleet --hub-url http://HUB-HOST:8765 --heatmap  # node × hour incident density
smoke incidents --hub-url http://HUB-HOST:8765        # the fleet-wide incident feed
```

or open `http://HUB-HOST:8765/` in a browser for the same thing as a dashboard.

repoint a node to a different hub later (writes `SMOKEMON_HUB_URL`, applied on the shipper's next run, <=15s):

```bash
smoke hub NEW-HUB-HOST
```

---

every flag and service → [INSTALL.md](INSTALL.md) · what it measures → [README.md](README.md) ·
how detection works → [docs/detector-spec.md](docs/detector-spec.md)
