#!/usr/bin/env bash
# Wipe every trace of an older smokemon from a set of hosts and install the current code.
#
# This is deliberately a reprovision, not an update. Two reasons a plain `update.sh` will not
# work here:
#
#   * The repository history was squashed, so an existing /opt/smokemon checkout has no common
#     ancestor with origin/main. install.sh's `git pull --ff-only` fails on every host that
#     already has one, and it fails AFTER it has started, leaving a half-updated node.
#   * The old schema is gone. Nothing in an old node database is readable by the new code, and
#     nothing in it is worth reading.
#
# Usage, run from the hub (where the secret is already on disk and the API is local):
#   scripts/fleet-reprovision.sh --yes host1 host2 ...
#   scripts/fleet-reprovision.sh --yes --hosts-file nodes.txt --jobs 6
#
# From anywhere else, pass --secret S explicitly.
#
# Destructive steps require --yes. Without it the script prints exactly what it would run.
set -euo pipefail

HUB_URL="${SMOKEMON_HUB_URL:-http://100.127.203.7:8765/ingest}"
HUB_API="${HUB_URL%/ingest}"
SECRET="${SMOKEMON_SECRET:-}"
SSH_USER="${SSH_USER:-}"
SSH_OPTS="${SSH_OPTS:--o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"
REPO_RAW="${SMOKEMON_REPO_RAW:-https://raw.githubusercontent.com/oovets/smokemon/main/install.sh}"
JOBS=4
YES=0
LIMIT=0
TARGETS_ARG=""
# Index-aligned. NAMES is what the node is called on the hub; ADDRS is how we reach it. They
# are kept apart on purpose: SSH goes to the Tailscale IP, which works whether or not MagicDNS
# is enabled, while the hub still shows a readable hostname instead of 100.x.y.z.
NAMES=()
ADDRS=()

die() { echo "error: $*" >&2; exit 1; }
log() { printf '%s\n' "$*" >&2; }

# The installer is sent over the wire, not fetched by the node. raw.githubusercontent caches
# per path per CDN edge, and appending a query string does not reliably defeat it -- a rollout
# started minutes after a push repeatedly ran the PREVIOUS installer while reporting success.
# Shipping the local copy also means the installer you can read is the one that ran.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _p in "$_here/../install.sh" /usr/local/lib/smokemon-install.sh; do
    [ -f "$_p" ] && { INSTALLER="$_p"; break; }
done
[ -n "${INSTALLER:-}" ] || die "cannot find install.sh (looked next to this script and in /usr/local/lib)"

add_host() {
    # "name=addr" separates what the node is called on the hub from how we reach it, for a box
    # whose tailnet name does not resolve from here or that is quicker to reach over the LAN.
    # A bare "host" means both.
    case "$1" in
        *=*) NAMES+=("${1%%=*}"); ADDRS+=("${1#*=}") ;;
        *)   NAMES+=("$1");       ADDRS+=("${2:-$1}") ;;
    esac
}

from_tailscale() {  # from_tailscale PATTERN
    command -v tailscale >/dev/null 2>&1 || die "tailscale not found; use --hosts-file instead"
    local out
    # Online peers only, Self never. Excluding Self is a safety property, not a nicety: run
    # this on the hub with a pattern that happens to match it and the script would tear down
    # smokemon-hub.service and reinstall the box as an ordinary node, mid-rollout.
    out="$(tailscale status --json | python3 -c '
import fnmatch, json, sys

pat = sys.argv[1]
d = json.load(sys.stdin)
me = (d.get("Self") or {}).get("HostName", "")

by_name = {}
for p in (d.get("Peer") or {}).values():
    h = p.get("HostName") or ""
    if not h or h == me or not fnmatch.fnmatch(h, pat):
        continue
    ips = p.get("TailscaleIPs") or []
    online = bool(p.get("Online")) and bool(ips)
    # A tailnet accumulates stale registrations: a machine that re-joins appears twice, the
    # old entry offline and months out of date. Keep the online one. This is not cosmetic --
    # SMOKEMON_NODE defaults to the hostname and the hub keys everything on it, so two boxes
    # under one name would merge into a single node in every view.
    prev = by_name.get(h)
    if prev is None or (online and not prev[0]):
        by_name[h] = (online, ips[0] if ips else "")
    elif online and prev[0]:
        print(f"DUP\t{h}\t{prev[1]},{ips[0]}")

on  = sorted((h, ip) for h, (o, ip) in by_name.items() if o)
off = sorted(h for h, (o, _) in by_name.items() if not o)
for h, ip in on:
    print(f"ON\t{h}\t{ip}")
print(f"OFF\t{len(off)}\t" + ",".join(off))
' "$1")" || die "could not read tailscale status"
    local kind a b
    while IFS=$'\t' read -r kind a b; do
        case "$kind" in
            ON)  add_host "$a" "$b" ;;
            OFF) SKIPPED_N="$a"; SKIPPED="$b" ;;
            # Two live machines answering to one name is not something to resolve by guessing:
            # whichever we picked, the other would silently share its identity on the hub.
            DUP) die "two ONLINE peers both named '$a' ($b). Rename one in the tailnet, or list hosts explicitly." ;;
        esac
    done <<< "$out"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --secret)      SECRET="$2"; shift 2 ;;
        --hub-url)     HUB_URL="$2"; HUB_API="${HUB_URL%/ingest}"; shift 2 ;;
        --tailscale)   from_tailscale "$2"; shift 2 ;;
        --hosts-file)  while IFS= read -r _h; do
                           case "$_h" in ''|\#*) continue ;; esac
                           add_host "$_h"
                       done < "$2"; shift 2 ;;
        --ssh-user)    SSH_USER="$2"; shift 2 ;;
        --targets)     TARGETS_ARG="--targets $2"; shift 2 ;;
        --jobs)        JOBS="$2"; shift 2 ;;
        --limit)       LIMIT="$2"; shift 2 ;;
        --yes)         YES=1; shift ;;
        -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
        -*)            die "unknown option: $1" ;;
        *)             add_host "$1"; shift ;;
    esac
done

SKIPPED_N="${SKIPPED_N:-0}"; SKIPPED="${SKIPPED:-}"
[ "${#NAMES[@]}" -gt 0 ] || die "no hosts (positional args, --hosts-file, or --tailscale PATTERN)"

if [ "$LIMIT" -gt 0 ] && [ "$LIMIT" -lt "${#NAMES[@]}" ]; then
    log "limiting to the first $LIMIT of ${#NAMES[@]} host(s)"
    NAMES=("${NAMES[@]:0:$LIMIT}")
    ADDRS=("${ADDRS[@]:0:$LIMIT}")
fi

# Run from the hub and the secret is already on this box. Reading it beats retyping it: a
# mistyped secret produces an install that looks entirely successful and then silently fails
# to ship, which is only caught minutes later by the hub-side check at the end.
if [ -z "$SECRET" ]; then
    if [ ! -e /etc/smokemon.env ]; then
        die "no secret and no /etc/smokemon.env.
  If this box is the hub, it was started by hand rather than installed. Install it properly:
    curl -fsSL https://raw.githubusercontent.com/oovets/smokemon/main/install.sh | sudo bash -s -- --hub
  Otherwise pass --secret explicitly."
    elif [ ! -r /etc/smokemon.env ]; then
        die "/etc/smokemon.env exists but is not readable as $(id -un) -- rerun with sudo."
    fi
    SECRET="$(sed -n 's/^SMOKEMON_HUB_SECRET=//p' /etc/smokemon.env | head -1)"
    [ -n "$SECRET" ] || die "/etc/smokemon.env has no SMOKEMON_HUB_SECRET line"
    log "using SMOKEMON_HUB_SECRET from /etc/smokemon.env"
fi

ssh_to() {  # ssh_to HOST COMMAND...
    local host="$1"; shift
    local target="$host"
    [ -n "$SSH_USER" ] && target="$SSH_USER@$host"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$target" "$@"
}

scp_to() {  # scp_to LOCAL_FILE HOST REMOTE_PATH
    local target="$2"
    [ -n "$SSH_USER" ] && target="$SSH_USER@$2"
    # shellcheck disable=SC2086
    scp $SSH_OPTS -q "$1" "$target:$3"
}

# Runs FIRST, before the installer is even copied up -- not just tidy but load-bearing. A
# disk-full node fails at the scp step (no space to write the file, before remote_script gets
# a chance to run), which is exactly what happened on aspace-prod-70: a host running low on
# root disk cannot be reprovisioned until something is freed, and no other step in this script
# runs early enough to do that.
#
# Three things, all standard and reversible: journald logs beyond 3 days (systemd usually caps
# this itself via SystemMaxUse=, but not every image sets it, and journals grow unbounded
# otherwise); apt's downloaded .deb cache (redownloaded on demand, never needed once installed);
# and DISABLED snap revisions -- snapd keeps the last few revisions of every package by default
# for rollback, and on a fleet running dozens of snaps (core22, go, snapd itself, ...) that is
# the single largest reclaimable chunk on disk. `snap list --all` marks the currently-active
# revision; only the others are removed.
housekeeping_cmd() {
    cat <<'HOUSEKEEPING'
journalctl --vacuum-time=3d >/dev/null 2>&1 || true
apt-get clean >/dev/null 2>&1 || true
if command -v snap >/dev/null 2>&1; then
    snap list --all 2>/dev/null | awk '/disabled/{print $1, $3}' | while read -r name rev; do
        snap remove "$name" --revision="$rev" >/dev/null 2>&1 || true
    done
fi
HOUSEKEEPING
}

# The remote teardown + verification, run under sudo bash. install.sh itself is scp'd up
# separately and invoked by path -- embedding a few hundred lines of it inside a heredoc was
# fragile to quote and added nothing an ordinary file copy does not already do better.
remote_script() {
    local node="$1"
    cat <<REMOTE
set -euo pipefail

echo "-- forcing everything smokemon off this box first"
# Deliberately a purge rather than trusting the installer's own teardown. An install that
# starts from a known-empty box has one outcome; an install layered onto whatever the previous
# version left behind has as many outcomes as there are previous versions.
systemctl stop 'smokemon*' 2>/dev/null || true
for u in \$(systemctl list-unit-files --no-legend 'smokemon*' 2>/dev/null | awk '{print \$1}'); do
    systemctl disable "\$u" 2>/dev/null || true
done
rm -f /etc/systemd/system/smokemon*.service /etc/systemd/system/smokemon*.timer
rm -f /etc/systemd/system/*.wants/smokemon*
systemctl daemon-reload
# A unit deleted while failed leaves a "not-found failed" entry that shows up in list-units
# forever and matches any health check grepping for failures.
systemctl reset-failed 'smokemon*' 2>/dev/null || true
pkill -f 'smokemon' 2>/dev/null || true
rm -rf /opt/smokemon /usr/local/lib/smokemon.pyz /usr/local/bin/smoke /usr/local/bin/smokeincidents
rm -f /etc/profile.d/smokemon.sh
# Old node databases. Nothing the previous schema wrote is readable by the current code, and
# the set-aside copies occupy exactly the SD-card space this design exists to save. Guarded on
# data/ rather than globbing rm -rf across every home: a coincidentally named ~/smokemon that
# is not a smokemon data directory must survive this.
for d in /root/smokemon /home/*/smokemon; do
    [ -d "\$d/data" ] || continue
    echo "   removing \$d"
    rm -rf "\$d"
done
rm -rf /var/lib/smokemon

echo "-- installing"
bash /tmp/smokemon-install.sh --node "$node" --hub-url "$HUB_URL" --secret "$SECRET" $TARGETS_ARG
rm -f /tmp/smokemon-install.sh

echo "-- verification"
sleep 3
fail=0
if systemctl is-active --quiet smokemon.service; then
    echo "   smokemon.service active"
else
    echo "   smokemon.service NOT ACTIVE"; fail=1
fi
# A probe that dies on its first cycle is the failure a rollout is most likely to hit, and it
# is invisible from the hub: a node that never starts also never ships anything to be missed.
if journalctl -u smokemon --since "-60s" --no-pager 2>/dev/null | grep -qE "probe .* failed"; then
    echo "   PROBE ERRORS:"
    journalctl -u smokemon --since "-60s" --no-pager | grep -E "probe .* failed" | tail -5 | sed 's/^/     /'
    fail=1
fi
exit \$fail
REMOTE
}

summary() {
    log "hub:     $HUB_URL"
    log "user:    ${SSH_USER:-<ssh default>}"
    log "jobs:    $JOBS"
    log "hosts:   ${#NAMES[@]}"
    local i
    for i in "${!NAMES[@]}"; do log "   ${NAMES[$i]}  (${ADDRS[$i]})"; done
    # Offline peers are reported rather than silently dropped: after a rollout it must be
    # obvious which boxes still carry the old code, or they turn into a quiet long tail.
    #
    # An `if`, not `[ ] && { }`: under `set -e` a trailing false test makes the function return
    # non-zero and kills the script. That fired whenever nothing was skipped -- which is every
    # run that names hosts explicitly, i.e. every canary.
    if [ "$SKIPPED_N" -gt 0 ]; then
        log ""
        log "skipped $SKIPPED_N offline peer(s); rerun later to catch them, e.g."
        log "   $(printf '%s' "$SKIPPED" | tr ',' ' ' | cut -c1-70)..."
    fi
}

if [ "$YES" -ne 1 ]; then
    log "DRY RUN -- nothing will be changed. Re-run with --yes to execute."
    log ""
    summary
    log ""
    log "For each host: housekeeping (journal/apt/snap cleanup), then scp $INSTALLER, then as root:"
    remote_script "<hostname>" | sed 's/^/    /' >&2
    exit 0
fi

summary
log ""
log "reprovisioning ${#NAMES[@]} host(s)"
mkdir -p .fleet-logs
# Taken BEFORE the installs, not after. A node writes its first heartbeat as the daemon starts,
# which is during this loop -- anchoring the freshness cut afterwards discards that one and
# leaves the check waiting a full heartbeat interval for the next.
START="$(date +%s)"
# Fixed-size batches rather than a sliding window: `wait -n` needs bash 4.3, and this may well
# be invoked from a Mac, where bash is 3.2.
i=0
while [ "$i" -lt "${#NAMES[@]}" ]; do
    for j in $(seq "$i" $(( i + JOBS - 1 ))); do
        [ "$j" -lt "${#NAMES[@]}" ] || break
        (
            name="${NAMES[$j]}"; addr="${ADDRS[$j]}"
            out=".fleet-logs/$name.log"
            echo "-- housekeeping (logs, apt cache, disabled snap revisions)" >"$out"
            housekeeping_cmd | ssh_to "$addr" "sudo bash -s" >>"$out" 2>&1 || true
            # scp lands in the login user's home (root has no writable /tmp over scp with
            # BatchMode in some sshd configs, home always works); the remote script sudo-moves
            # it before running so it works whether that user is root or not.
            if scp_to "$INSTALLER" "$addr" "smokemon-install.sh" >>"$out" 2>&1 \
               && remote_script "$name" | ssh_to "$addr" \
                    "sudo mv ~/smokemon-install.sh /tmp/smokemon-install.sh && sudo bash -s" \
                    >>"$out" 2>&1; then
                log "  OK    $name"
            else
                log "  FAIL  $name   (see $out)"
            fi
        ) &
    done
    wait
    i=$(( i + JOBS ))
done

# The local check proves the agent runs; only the hub proves it is being heard. A healthy node
# writes nothing but its heartbeat, so give it one heartbeat interval plus slack to appear.
log ""
log "waiting for nodes to report to the hub"
deadline=$(( $(date +%s) + 420 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    # Only nodes whose newest heartbeat postdates the rollout count. Asking merely "is this
    # node known to the hub" passes for a host that was already reporting before we touched
    # it, so a reinstall that never ran -- an unreachable box, a failed ssh -- reports success.
    seen="$(curl -fsS --max-time 10 "$HUB_API/api/fleet" 2>/dev/null \
            | START="$START" python3 -c '
import json, os, sys
cut = float(os.environ["START"])
d = json.load(sys.stdin)
print(" ".join(n["node"] for n in d.get("fleet", [])
                if (n.get("heartbeat") or {}).get("ts", 0) >= cut))' 2>/dev/null || true)"
    missing=()
    for h in "${NAMES[@]}"; do
        case " $seen " in *" $h "*) ;; *) missing+=("$h") ;; esac
    done
    [ "${#missing[@]}" -eq 0 ] && { log "all ${#NAMES[@]} node(s) reporting"; exit 0; }
    log "   ${#missing[@]} of ${#NAMES[@]} still quiet"
    sleep 20
done
log "still missing after 7 min: ${missing[*]}"
log "  check:  ssh HOST journalctl -u smokemon-shipper --since -10min"
exit 1
