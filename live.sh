#!/bin/zsh
# smokemon - live TUI view, redraws the text graph in the terminal on an interval.
# Usage: live.sh [window] [refresh_sec]
#   live.sh            # last 15 min, refresh every 10s
#   live.sh 24h        # last 24 hours (trend view)
#   live.sh 90m 30     # last 90 min, refresh every 30s
#   live.sh 5 5        # last 5 min, refresh every 5s (bare number = minutes)
PY=/opt/anaconda3/bin/python3
DIR="${0:A:h}"
WIN="${1:-15m}"
REFRESH="${2:-10}"

# Parse the window -> term_plot.py flag + header label
case "$WIN" in
    *h) FLAG=(--hours "${WIN%h}");   LABEL="${WIN%h}h" ;;
    *m) FLAG=(--minutes "${WIN%m}"); LABEL="${WIN%m} min" ;;
    *)  FLAG=(--minutes "$WIN");     LABEL="$WIN min" ;;
esac

# Kiosk mode (SMOKEMON_KIOSK=1): clean graphs, no header, no axes/legend
if [[ -n "$SMOKEMON_KIOSK" ]]; then
    EXTRA=(--kiosk); RESERVE=0; HEADER=0
else
    EXTRA=(); RESERVE=2; HEADER=1
fi

cleanup() { tput cnorm; printf '\n'; exit 0 }
trap cleanup INT TERM
tput civis  # hide cursor

while true; do
    clear
    [[ "$HEADER" == 1 ]] && printf "smokemon LIVE — last %s · refresh %ss · %s · Ctrl-C to quit\n" \
        "$LABEL" "$REFRESH" "$(date '+%H:%M:%S')"
    "$PY" "$DIR/term_plot.py" "${FLAG[@]}" "${EXTRA[@]}" --reserve "$RESERVE" 2>/dev/null \
        || printf "(waiting for data...)\n"
    sleep "$REFRESH"
done
