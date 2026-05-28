# Security Policy

## Supported versions

Only the `main` branch and the most recent tagged release receive security fixes. There is no LTS branch.

| Version | Supported |
|---------|-----------|
| latest tag (0.11.x) | yes |
| < 0.11 | no |

## Reporting a vulnerability

**Do not file public GitHub issues for security problems.**

Report via one of:

1. **GitHub Security Advisory** (preferred): https://github.com/oovets/smokemon/security/advisories/new
2. **Email**: `stefan@weapply.se` with the subject line `smokemon security:` and a clear description. Encrypted reports welcome on request.

Please include:

- Affected version (git SHA or tag).
- Reproduction steps or proof-of-concept.
- Impact assessment (what an attacker gains).
- Suggested remediation, if you have one.

You will get an acknowledgement within 3 working days. We aim to ship a fix within 30 days for high-severity issues, longer for low-severity. Credit is given in the release notes unless you ask otherwise.

## Known security-relevant surfaces

These are areas we already know are sensitive. Reports about them are welcome; we have made the trade-offs documented but new attack angles are worth flagging.

- **Hub ingest is HTTP without TLS** (`smokemon.hub`). The shared secret travels in the `X-Smokemon-Key` header in cleartext. We assume the hub is exposed only over Tailscale, WireGuard, or another private L3 link. Default bind is `0.0.0.0` for ease of setup - production deployments should override `SMOKEMON_HUB_BIND` to a specific interface.
- **mtr with `sudo -n` on macOS** (`smokemon.probes.mtr`). The install instructions add a NOPASSWD sudoers rule for `mtr` only. On Linux this is avoided entirely via `setcap cap_net_raw`.
- **Subprocess arguments come from env-vars** (`SMOKEMON_TARGETS`, `SMOKEMON_MTR_TARGETS`, etc.). Argv lists are passed without `shell=True`, but an operator who sets a malicious env-var still has shell access to the box; treat env-var contents as trusted.
- **No replay protection** on ingest currently. A captured payload can be replayed and is silently absorbed by `INSERT OR IGNORE` on the `UNIQUE(node, src_id)` index, but a malicious node with the secret can backfill old data. Discussed in the v0.12 roadmap.
- **SQLite WAL on a shared host** is readable by anything with file-system access. No row-level encryption is performed.

## Out of scope

- Denial of service against the public internet by configuring smokemon to probe it heavily (you are responsible for what you measure).
- Vulnerabilities in `fping`, `mtr`, `curl`, `iperf3`, `iw`, `system_profiler`, or the kernel.
- Vulnerabilities in `matplotlib`, `numpy`, or `plotext` (please report to those projects).
