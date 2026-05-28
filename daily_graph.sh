#!/bin/zsh
# smokemon - save a dated PNG of the last 24h. Run daily by launchd.
set -e
PY=/opt/anaconda3/bin/python3
DIR="${0:A:h}"
OUTDIR="$DIR/graphs/daily"
mkdir -p "$OUTDIR"
"$PY" "$DIR/plot.py" --hours 24 --out "$OUTDIR/smokemon-$(date +%F).png" --no-open
