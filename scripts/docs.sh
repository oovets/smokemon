#!/usr/bin/env bash
# Stage the repo-root markdown into docs/ for MkDocs, keeping the source of truth at
# the root. README.md becomes the site home (index.md). Run before `mkdocs build` or
# `mkdocs serve`; CI (.github/workflows/docs.yml) runs it too.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p docs
cp README.md docs/index.md
for f in QUICKSTART.md INSTALL.md PLAN.md CHANGELOG.md CONTRIBUTING.md SECURITY.md; do
    [ -f "$f" ] && cp "$f" "docs/$f"
done

# Rewrite repo-relative links for the SITE BUILD ONLY (the root .md keep their GitHub-correct
# links). README.md is the site home, so it becomes index.md; files that are not staged into
# docs/ (LICENSE, pyproject.toml) point at the GitHub blob instead. Keeps `mkdocs build
# --strict` clean - any remaining "link not found" warning is then a real broken link.
blob="https://github.com/oovets/smokemon/blob/main"
for f in docs/*.md; do
    sed -i \
        -e 's#](README\.md#](index.md#g' \
        -e "s#](pyproject\.toml)#](${blob}/pyproject.toml)#g" \
        -e "s#](LICENSE)#](${blob}/LICENSE)#g" \
        "$f"
done

# Tag bare opening fences as ```bash for the SITE BUILD ONLY (the root .md stays clean on
# GitHub). This gives every code box Pygments syntax colouring like the already-tagged
# CONTRIBUTING blocks. Toggles on each fence line so closing fences are left untouched.
for f in docs/*.md; do
    awk '
      /^```/ {
        if (!inblock) { inblock=1; if ($0 == "```") { print "```bash"; next } }
        else { inblock=0 }
        print; next
      }
      { print }
    ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done

# Stamp the latest commit date + short SHA onto the rendered home page so the site shows
# when it was last updated (site build only; README.md on GitHub stays clean).
stamp="last updated: $(git log -1 --format=%cd --date=short) (commit $(git rev-parse --short HEAD))"
printf '\n\n```\n%s\n```\n' "$stamp" >> docs/index.md

echo "staged docs/: $(cd docs && ls *.md | tr '\n' ' ')"
