---
name: Bug report
about: Something doesn't work the way it should
title: "[bug] "
labels: bug
---

## What happened

(One or two sentences. What command did you run, what did you expect, what did you see?)

## Reproduction

```bash
# exact commands that trigger the problem
```

## Environment

- OS: (e.g. macOS 14.5, Raspberry Pi OS Bookworm, Ubuntu 24.04, JetPack 6.0)
- Python: (`python3 --version`)
- smokemon version or git SHA: (`git -C smokemon rev-parse --short HEAD`)
- Deployment mode: single host / node + hub / something else

## Logs

```
# launchd:   ~/smokemon/logs/*.err.log
# systemd:   journalctl -u smokemon-* --since "1 hour ago"
```

## Additional context

Anything else that might be relevant - hardware, network topology, recent changes, etc.
