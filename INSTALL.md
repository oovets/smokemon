# smokemon — install & reference

full install and operations reference for smokemon. the short version lives in
[README.md](README.md); this is the detailed one. smokemon is a `smokemon/` python package: collectors, shipper and hub are stdlib-only, the renderers add plotext (TUI) or matplotlib+numpy (PNG). a node runs two long-lived collector daemons (`collect fast` = ping+net @10s, `collect slow` = http+mtr+wifi+host @60s/30s) plus two timers (`iperf` @15min, `ship` @60s). a hub runs one process (`smokemon.hub`) that ingests delta batches the nodes push to it. everything is driven by launchd (macOS) or systemd (Linux); nothing daemonizes itself.

```
ONE NODE (local only)
  collect fast (ping+net)  --\
  collect slow (http/mtr/   --|-> data/smokemon.db -> smoke tui / smoke png
               wifi/host)    --|
  iperf (timer)            --/

MULTI-NODE + CENTRAL HUB (push model, Tailscale-friendly)
  node1: collect+iperf -> smokemon.db -> ship --\
  node2: collect+iperf -> smokemon.db -> ship ---} POST /ingest  (X-Smokemon-Key)
  node3: collect+iperf -> smokemon.db -> ship --/
                                                 v
                              smokemon.hub  (:8765, ThreadingHTTPServer)
                                                 v
                                    data/smokemon-hub.db
                                                 v
                              smoke png/tui --node NAME

PACKAGE LAYOUT
  smokemon/ config core schema collect ship hub query cli
            adapters/{darwin,linux}
            probes/{ping,net,http,mtr,wifi,iperf,host}
            render/{tui,png}
  deploy/   launchd/*.plist   systemd/*
  install.sh   (repo root; works local or curl-piped)
```

schema is single-source (`schema.py`): node DDL, hub DDL (adds `node` + `src_id` +
`UNIQUE(node,src_id)`), `STD_TABLES` and the generic INSERT all derive from one table
spec. migrations are additive (`ensure_node_column`), so the node DB and the hub DB share
one schema and one plotter codebase. storage is SQLite WAL with no pruning (~5–6 GB/yr per
node; if it matters, roll up to a lower resolution — never delete raw data blindly).
footprint is ~30 MB RSS per node (two daemons) and well under 1% of one core; the hub adds
~20 MB.

```
node:  python3 >=3.10 (stdlib only for collection); plotext for the local TUI.
       macOS:  brew install fping mtr iperf3   (curl/netstat/ifconfig/system_profiler built in)
       Linux:  apt install fping mtr-tiny iperf3 iw
hub:   python3 >=3.10 + matplotlib + numpy (PNG); iperf3 -s if nodes test bandwidth to it.
net:   prefer Tailscale/VPN between node and hub. The hub is plain HTTP (no TLS) on
       8765/tcp — bind it to a private address only.
```

macos single host (test run, no launchd):

```
git clone <repo> ~/smokemon && cd ~/smokemon
brew install fping mtr iperf3 && python3 -m pip install --user plotext
PYTHONPATH=. python3 -m smokemon.collect all &      # all probes in one process
PYTHONPATH=. python3 -m smokemon.cli live 6h        # live TUI, last 6h
```

linux single host (one-liner; clones to /opt/smokemon, installs systemd units):

```
curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh \
    | sudo bash -s -- --node "$(hostname)"
# (no --hub-url => local only, nothing shipped)
```

multi-node + central hub:

```
# hub (once):
curl -fsSL .../install.sh | sudo bash -s -- --hub --secret MY_SECRET
# each node:
curl -fsSL .../install.sh | sudo bash -s -- --node NAME \
    --hub-url http://HUB-HOST:8765/ingest --secret MY_SECRET
# on the hub, plot per node:
PYTHONPATH=/opt/smokemon python3 -m smokemon.cli png \
    --db /opt/smokemon/data/smokemon-hub.db --node NAME --hours 24
```

the plists run `python3 -m smokemon.*` with `WorkingDirectory` + `PYTHONPATH` set to the
repo, and include a `PATH` with `/opt/homebrew/{bin,sbin}` so `shutil.which` finds
fping/mtr/iperf3. no pip install required.

```
brew install fping mtr iperf3 && python3 -m pip install --user plotext
git clone <repo> ~/smokemon && cd ~/smokemon && mkdir -p data logs

# mtr needs root -> passwordless sudo for the exact binary:
sudo tee /etc/sudoers.d/smokemon >/dev/null <<EOF
$(whoami) ALL=(root) NOPASSWD: $(command -v mtr)
EOF

cp deploy/launchd/*.plist ~/Library/LaunchAgents/
for s in collect-fast collect-slow iperf daily; do
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stefan.smokemon-$s.plist
done
# optional shipper (edit SMOKEMON_HUB_URL + SMOKEMON_HUB_SECRET first) and hub:
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stefan.smokemon-{shipper,hub}.plist
```

services: `collect-fast` (RunAtLoad+KeepAlive), `collect-slow` (KeepAlive), `iperf`
(StartInterval 900s), `daily` (`smoke daily` at 23:55), `shipper` (StartInterval 60s,
optional), `hub` (KeepAlive, optional).

`install.sh` does it all: apt deps, `setcap cap_net_raw+ep` on fping/mtr-packet (so mtr
needs no sudo), `pip --user plotext`, writes `/etc/smokemon.env`, drops `smoke`/`smokelive`/
`smokekiosk`/`smokepng` into `/etc/profile.d/smokemon.sh` (new login shells), and
template-substitutes + enables the systemd units.

```
sudo ./install.sh --node NAME [--hub-url http://HUB-HOST:8765/ingest --secret S] [--targets a,b]
# or piped, see Quickstart. Units enabled:
#   smokemon-collect-fast.service   ping/net, always on
#   smokemon-collect-slow.service   http/mtr/wifi/host, always on
#   smokemon-iperf.timer            iperf every 15 min
#   smokemon-shipper.timer          ship every 60s
```

```
# Linux:
sudo ./install.sh --hub --secret SHARED_SECRET
#   apt: iperf3 + python3-matplotlib + python3-numpy; writes /etc/smokemon.env
#   (SMOKEMON_HUB_DB, HUB_BIND=0.0.0.0, HUB_PORT=8765, HUB_SECRET); enables smokemon-hub.

# macOS: use deploy/launchd/com.stefan.smokemon-hub.plist (set SMOKEMON_HUB_SECRET, bind a
# private address), then launchctl bootstrap it.
```

the hub listens on 8765/tcp, `POST /ingest` only, header `X-Smokemon-Key`. if the secret is
the default `changeme` it logs a warning at startup. expose the port only over a private
network — there is no TLS.

set in the launchd plist `EnvironmentVariables` (macOS) or `/etc/smokemon.env` (Linux).

```
general
  SMOKEMON_DB              local SQLite DB        (default <repo>/data/smokemon.db)
  SMOKEMON_NODE            node name              (default hostname)

ping + net (collect fast)
  SMOKEMON_TARGETS         comma-sep ping targets (default 1.1.1.1,192.168.0.1)
  SMOKEMON_INTERVAL        seconds/cycle          (default 10)
  SMOKEMON_COUNT           pings/cycle/target     (default 20)
  SMOKEMON_PERIOD          ms between pings        (default 50)
  SMOKEMON_FPING           fping path             (fallback: PATH lookup)

http + mtr + wifi (collect slow)
  SMOKEMON_PROBE_INTERVAL  seconds/cycle          (default 60)
  SMOKEMON_HTTP_URLS       comma-sep URLs         (default google.com, cloudflare.com)
  SMOKEMON_MTR_TARGETS     comma-sep mtr targets  (default 1.1.1.1)
  SMOKEMON_MTR_COUNT       pings/mtr run          (default 10)
  SMOKEMON_MTR_SUDO        1 = sudo -n mtr, 0 = direct (set 0 on Linux after setcap)
  SMOKEMON_WIFI            1/0 enable WiFi probe  (default 1)
  SMOKEMON_CURL, SMOKEMON_MTR   tool paths        (fallback: PATH lookup)

host (collect slow)
  SMOKEMON_HOST_INTERVAL   seconds/sample         (default 30)
  SMOKEMON_PROC_TOPN       top-CPU procs kept     (default 5)

iperf (probes.iperf)
  SMOKEMON_IPERF_SERVER    iperf3 -s host         (unset -> probe no-ops)
  SMOKEMON_IPERF_DURATION  seconds/direction      (default 5)
  SMOKEMON_IPERF           iperf3 path            (fallback: PATH lookup)

ship (push -> hub)
  SMOKEMON_HUB_URL         hub /ingest URL        (unset -> ship no-ops)
  SMOKEMON_HUB_SECRET      shared secret          (default changeme - CHANGE)
  SMOKEMON_SHIP_BATCH      max rows/batch/table   (default 2000)
  SMOKEMON_SHIP_INTERVAL   loop seconds; 0 = drain once and exit (for a timer)

hub (smokemon.hub)
  SMOKEMON_HUB_DB          hub DB path            (default <home>/smokemon/data/smokemon-hub.db)
  SMOKEMON_HUB_BIND        listen address         (default 0.0.0.0)
  SMOKEMON_HUB_PORT        port                   (default 8765)
  SMOKEMON_HUB_SECRET      shared secret          (must match the nodes)
  SMOKEMON_HUB_MAX_BODY    max POST bytes         (default 64 MiB)
```

run as `smoke <sub>` (zsh helper) or `python -m smokemon.cli <sub>` (`PYTHONPATH`=repo).
default sub is `tui`.

```
common flags (all subcommands):
  --db PATH  --hours N | --minutes N | --since ISO --until ISO
  --targets a,b,c  --panels ping,net,http,mtr,wifi,iperf,host,disk|all
  --node NAME    (REQUIRED when reading a hub DB)

smoke tui            static TUI                       (+ --kiosk, --reserve N)
smoke live [win]     redraw on interval; win = Nh/Nm/number(min); --refresh N (default 10)
smoke kiosk [win]    live + clean (no legend/axes/title/header)
smoke png            matplotlib PNG; --out, --width INCHES (0=auto), --dpi N (96), --no-open
smoke daily          dated 24h PNG -> graphs/daily/smokemon[-NODE]-YYYY-MM-DD.png

daemons (launchd/systemd, or by hand with PYTHONPATH=repo):
  python -m smokemon.collect fast | slow | all
  python -m smokemon.probes.iperf      one iperf3 up+down sample
  python -m smokemon.ship              drain deltas to the hub
  python -m smokemon.hub               run the ingest server
```

every table has a `node` column (default `SMOKEMON_NODE`/hostname). the hub DB additionally
has `src_id` + `UNIQUE(node, src_id)` per table for idempotent ingest.

```
ping_runs      one row per fping cycle (target, time, counts, loss%, rtt aggregates)
ping_rtts      every individual RTT (run_id -> ping_runs.id)
net_samples    cumulative byte counters per interface, per cycle
http_samples   DNS / connect / TLS / TTFB / total ms per URL
mtr_hops       per-hop loss / avg / best / worst / stddev
wifi_samples   RSSI / noise / tx-rate / channel
iperf_samples  up + down Mbit/s + retransmits
host_samples   CPU% / load / mem% / temp / disk IO
disk_samples   used% + free GB per mount
proc_samples   top-N processes by CPU
ship_state     (node DB only) shipper cursor per table
```

```
services   macOS: launchctl list | grep smokemon      Linux: systemctl status 'smokemon-*'
logs       macOS: ~/smokemon/logs/*.{out,err}.log      Linux: journalctl -u smokemon-collect-fast -f
reload     macOS: launchctl bootout/bootstrap gui/$(id -u) <plist>   (wait out the old one)
           Linux: systemctl restart smokemon-collect-fast
ship now   Linux: sudo systemctl start smokemon-shipper.service

hub up?    no /healthz; a POST without the key returns 401, proving it listens:
           curl -s -o /dev/null -w '%{http_code}\n' -X POST http://HUB:8765/ingest
           (401 = up, 200 = accepted, 413 = body too big)   or: ss -ltnp | grep 8765

no mtr/iperf on macOS   the launchd PATH must include /opt/homebrew/{bin,sbin} (the plists
                        set it) so shutil.which finds the Homebrew binaries.
no mtr on Linux         getcap "$(command -v mtr-packet)" should show cap_net_raw+ep; set
                        SMOKEMON_MTR_SUDO=0 in /etc/smokemon.env.
no wifi on Linux        `iw dev` must list a wireless iface and /proc/net/wireless be non-empty.
db growth               ~5-6 GB/yr; aggregate to lower resolution, do not delete raw data.
```

```
macOS
  for p in ~/Library/LaunchAgents/com.stefan.smokemon*.plist; do
      launchctl bootout gui/$(id -u) "$p"; rm -f "$p"
  done                                  # data/ and logs/ remain

Linux
  sudo systemctl disable --now smokemon-collect-fast smokemon-collect-slow \
      smokemon-shipper.timer smokemon-iperf.timer smokemon-hub 2>/dev/null
  sudo rm -f /etc/systemd/system/smokemon-*.{service,timer} /etc/smokemon.env
  sudo systemctl daemon-reload          # data/ remains in the repo dir
```
