#!/usr/bin/env bash
# Build the single-file agent. The package is stdlib-only, so a zipapp is a complete,
# runnable copy of smokemon in one file -- which is what makes "deploy" a scp.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-dist/smokemon.pyz}"
mkdir -p "$(dirname "$OUT")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# Copy the package only -- no tests, no docs, no .git. find|cpio would preserve pycache; a
# plain cp then a prune is clearer and the result is byte-identical.
cp -r smokemon "$STAGE/"
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete
python3 -m zipapp "$STAGE" -m "smokemon.__main__:main" -p "/usr/bin/env python3" -o "$OUT" -c
chmod +x "$OUT"
printf '%s  %s\n' "$(du -h "$OUT" | cut -f1)" "$OUT"
