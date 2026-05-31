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
# the package + probes deep-dives live next to the code (smokemon/); stage them as top-level
# pages so the site can link to them, same source-of-truth pattern as the root .md above.
[ -f smokemon/README.md ] && cp smokemon/README.md docs/package.md
[ -f smokemon/probes/README.md ] && cp smokemon/probes/README.md docs/probes.md

# Rewrite repo-relative links for the SITE BUILD ONLY (the root .md keep their GitHub-correct
# links). README.md is the site home, so it becomes index.md; files that are not staged into
# docs/ (LICENSE, pyproject.toml) point at the GitHub blob instead. Keeps `mkdocs build
# --strict` clean - any remaining "link not found" warning is then a real broken link.
blob="https://github.com/oovets/smokemon/blob/main"
for f in docs/*.md; do
    sed -i \
        -e 's#](README\.md#](index.md#g' \
        -e 's#](smokemon/probes/README\.md)#](probes.md)#g' \
        -e 's#](smokemon/README\.md)#](package.md)#g' \
        -e 's#](\.\./README\.md)#](index.md)#g' \
        -e 's#](\.\./INSTALL\.md)#](INSTALL.md)#g' \
        -e 's#](probes/README\.md)#](probes.md)#g' \
        -e "s#](pyproject\.toml)#](${blob}/pyproject.toml)#g" \
        -e "s#](LICENSE)#](${blob}/LICENSE)#g" \
        "$f"
done

# SITE BUILD ONLY (the root .md stay clean on GitHub). One python pass does two things:
#   1. GitHub alert blockquotes ( > [!WARNING] ... ) -> Material admonitions ( !!! warning ),
#      so the same callout renders natively on GitHub AND as a styled admonition on the site.
#   2. Bare ``` fences get a Pygments lexer chosen by content: a leading `$ ` -> console (prompt
#      colouring), a shell-command block -> bash, and ASCII reference panels / aligned tables ->
#      text (left flat so highlighting never mangles the box art). Already-tagged fences
#      (```bash / ```diff / ```console ...) are copied through untouched.
python3 - <<'PY'
import glob, re

SHELL = {"curl", "sudo", "git", "python", "python3", "pip", "brew", "cp", "sed", "awk", "for",
         "while", "ssh", "launchctl", "systemctl", "ss", "getcap", "mkdir", "tee", "alias",
         "export", "smoke", "smokelive", "smokekiosk", "smokepng", "journalctl", "scp", "bash",
         "sh", "cd", "ls", "cat", "echo", "sysctl", "setcap", "rm", "chmod", "chown", "apt"}
BOX = set("│┌┐└┘├┤┬┴┼─►▶▸→╴╶")
ALERT = {"NOTE": "note", "TIP": "tip", "IMPORTANT": "info", "WARNING": "warning", "CAUTION": "danger"}
alert_re = re.compile(r"^>\s*\[!(\w+)\]\s*$")


def choose(block):
    content = [l for l in block if l.strip()]
    if not content:
        return "text"
    first = content[0].lstrip()
    if first.startswith("$ "):
        return "console"
    joined = "\n".join(block)
    if "==" in joined or any(c in joined for c in BOX):
        return "text"                                  # ASCII reference panel / diagram
    gappy = sum(1 for l in content if re.search(r"\S {2,}\S", l))
    if gappy * 2 >= len(content):
        return "text"                                  # aligned label/description table
    return "bash" if re.split(r"\s+", first)[0] in SHELL else "text"


for path in glob.glob("docs/*.md"):
    lines = open(path, encoding="utf-8").read().split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        m = alert_re.match(line)
        if m and m.group(1).upper() in ALERT:
            i += 1
            body = []
            while i < n and lines[i].startswith(">"):
                body.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            out.append(f"!!! {ALERT[m.group(1).upper()]}")
            out.append("")
            out.extend(("    " + b) if b.strip() else "" for b in body)
            out.append("")
            continue
        if line.lstrip().startswith("```"):
            opener = line.strip()
            block, j = [], i + 1
            while j < n and not lines[j].lstrip().startswith("```"):
                block.append(lines[j])
                j += 1
            out.append(opener if opener != "```" else "```" + choose(block))
            out.extend(block)
            if j < n:
                out.append(lines[j])
            i = j + 1
            continue
        out.append(line)
        i += 1
    open(path, "w", encoding="utf-8").write("\n".join(out))
PY

# Stamp the latest commit date + short SHA onto the rendered home page so the site shows
# when it was last updated (site build only; README.md on GitHub stays clean).
stamp="last updated: $(git log -1 --format=%cd --date=short) (commit $(git rev-parse --short HEAD))"
printf '\n\n```\n%s\n```\n' "$stamp" >> docs/index.md

echo "staged docs/: $(cd docs && ls *.md | tr '\n' ' ')"
