#!/usr/bin/env bash
# Stage the repo-root markdown into docs/ for MkDocs, keeping the source of truth at
# the root. README.md becomes the site home (index.md). Run before `mkdocs build` or
# `mkdocs serve`; CI (.github/workflows/docs.yml) runs it too.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p docs
cp README.md docs/index.md
for f in INSTALL.md PLAN.md CHANGELOG.md CONTRIBUTING.md SECURITY.md; do
    [ -f "$f" ] && cp "$f" "docs/$f"
done
echo "staged docs/: $(cd docs && ls *.md | tr '\n' ' ')"
