#!/bin/zsh
# smokemon - save a dated PNG of the last 24h. Run daily by launchd.
set -e
PY="${SMOKEMON_PY:-/opt/anaconda3/bin/python3}"
[[ -x "$PY" ]] || PY=python3   # Linux/övriga: faller tillbaka till python3 på PATH
DIR="${0:A:h}"
OUTDIR="$DIR/graphs/daily"
mkdir -p "$OUTDIR"
"$PY" "$DIR/plot.py" --hours 24 --out "$OUTDIR/smokemon-$(date +%F).png" --no-open
