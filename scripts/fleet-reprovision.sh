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
TARGETS_ARG=""
HOSTS=()

die() { echo "error: $*" >&2; exit 1; }
log() { printf '%s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --secret)      SECRET="$2"; shift 2 ;;
        --hub-url)     HUB_URL="$2"; HUB_API="${HUB_URL%/ingest}"; shift 2 ;;
        --hosts-file)  while IFS= read -r _h; do
                           case "$_h" in ''|\#*) continue ;; esac
                           HOSTS+=("$_h")
                       done < "$2"; shift 2 ;;
        --ssh-user)    SSH_USER="$2"; shift 2 ;;
        --targets)     TARGETS_ARG="--targets $2"; shift 2 ;;
        --jobs)        JOBS="$2"; shift 2 ;;
        --yes)         YES=1; shift ;;
        -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
        -*)            die "unknown option: $1" ;;
        *)             HOSTS+=("$1"); shift ;;
    esac
done

[ "${#HOSTS[@]}" -gt 0 ] || die "no hosts given (positional args or --hosts-file)"

# Run from the hub and the secret is already on this box. Reading it beats retyping it: a
# mistyped secret produces an install that looks entirely successful and then silently fails
# to ship, which is only caught minutes later by the hub-side check at the end.
if [ -z "$SECRET" ] && [ -r /etc/smokemon.env ]; then
    SECRET="$(sed -n 's/^SMOKEMON_HUB_SECRET=//p' /etc/smokemon.env | head -1)"
    [ -n "$SECRET" ] && log "using SMOKEMON_HUB_SECRET from /etc/smokemon.env"
fi
[ -n "$SECRET" ] || die "no secret: pass --secret, or run this on the hub where /etc/smokemon.env is readable"

ssh_to() {  # ssh_to HOST COMMAND...
    local host="$1"; shift
    local target="$host"
    [ -n "$SSH_USER" ] && target="$SSH_USER@$host"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$target" "$@"
}

# Everything that has to happen on the node, as one script so a dropped connection cannot leave
# it half-done. Runs under sudo bash on the remote.
remote_script() {
    local node="$1"
    cat <<REMOTE
set -euo pipefail

echo "-- stopping any existing smokemon units"
# Timers before services: a timer that fires during the teardown would restart what we just
# stopped and race the reinstall.
for u in smokemon-prune.timer smokemon-shipper.timer smokemon-iperf.timer \\
         smokemon-collect-fast.service smokemon-collect-slow.service smokemon-hub.service \\
         smokemon-shipper.service smokemon-prune.service smokemon-iperf.service \\
         smokemon-iperf-server.service; do
    systemctl stop "\$u" 2>/dev/null || true
    systemctl disable "\$u" 2>/dev/null || true
    rm -f "/etc/systemd/system/\$u"
done
systemctl daemon-reload

echo "-- removing old checkout and data"
# The checkout must go: its git history has no common ancestor with the current origin/main,
# so install.sh's fast-forward pull would fail here rather than replacing it.
rm -rf /opt/smokemon
# Node databases and their set-aside predecessors. Nothing the old schema wrote is readable by
# the current code, and leaving *.old-v* behind occupies the SD card we are trying to spare.
# /etc/smokemon.env is rewritten by install.sh.
#
# Guarded on data/ rather than globbing rm -rf across every home directory: a coincidentally
# named ~/smokemon that is not a smokemon data dir must survive this.
for d in /root/smokemon /home/*/smokemon; do
    [ -d "\$d/data" ] || continue
    echo "   removing \$d"
    rm -rf "\$d"
done

echo "-- installing current code"
curl -fsSL "$REPO_RAW" | bash -s -- \\
    --node "$node" --hub-url "$HUB_URL" --secret "$SECRET" $TARGETS_ARG

echo "-- local verification"
sleep 3
fail=0
for u in smokemon-collect-fast smokemon-collect-slow; do
    if systemctl is-active --quiet "\$u"; then
        echo "   \$u active"
    else
        echo "   \$u NOT ACTIVE"; fail=1
    fi
done
# A probe that dies on the first cycle is the failure this rollout is most likely to hit, and
# it is invisible from the hub until the node fails to ship anything at all.
if journalctl -u smokemon-collect-fast -u smokemon-collect-slow --since "-60s" --no-pager 2>/dev/null \\
     | grep -qE "probe .* (failed|crashed)"; then
    echo "   PROBE ERRORS in journal:"
    journalctl -u smokemon-collect-fast -u smokemon-collect-slow --since "-60s" --no-pager \\
      | grep -E "probe .* (failed|crashed)" | tail -5 | sed 's/^/     /'
    fail=1
fi
exit \$fail
REMOTE
}

if [ "$YES" -ne 1 ]; then
    log "DRY RUN -- nothing will be changed. Re-run with --yes to execute."
    log ""
    log "hub:     $HUB_URL"
    log "hosts:   ${HOSTS[*]}"
    log "jobs:    $JOBS"
    log ""
    log "On each host, as root:"
    remote_script "<hostname>" | sed 's/^/    /' >&2
    exit 0
fi

log "reprovisioning ${#HOSTS[@]} host(s) -> $HUB_URL"
mkdir -p .fleet-logs
# Fixed-size batches rather than a sliding window: `wait -n` needs bash 4.3, and this is most
# likely to be run from a laptop, where bash may well be 3.2.
i=0
while [ "$i" -lt "${#HOSTS[@]}" ]; do
    batch=("${HOSTS[@]:$i:$JOBS}")
    for host in "${batch[@]}"; do
        (
            out=".fleet-logs/$host.log"
            if remote_script "$host" | ssh_to "$host" "sudo bash -s" >"$out" 2>&1; then
                log "  OK    $host"
            else
                log "  FAIL  $host   (see $out)"
            fi
        ) &
    done
    wait
    i=$(( i + JOBS ))
done

# The local check proves the agent runs; only the hub proves it is being heard. A healthy node
# writes nothing but its heartbeat, so give it one heartbeat interval plus slack to appear.
log ""
log "waiting for nodes to appear on the hub (first heartbeat is up to 5 min away)"
deadline=$(( $(date +%s) + 420 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    seen="$(curl -fsS --max-time 10 "$HUB_API/api/fleet" 2>/dev/null \
            | python3 -c 'import json,sys;print(" ".join(n["node"] for n in json.load(sys.stdin).get("fleet",[])))' 2>/dev/null || true)"
    missing=()
    for h in "${HOSTS[@]}"; do
        case " $seen " in *" $h "*) ;; *) missing+=("$h") ;; esac
    done
    [ "${#missing[@]}" -eq 0 ] && { log "all ${#HOSTS[@]} node(s) reporting"; exit 0; }
    sleep 20
done
log "still missing after 7 min: ${missing[*]}"
log "  check:  ssh HOST journalctl -u smokemon-shipper --since -10min"
exit 1
