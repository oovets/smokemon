# security

report privately — never a public github issue.
security advisory -> [open one](https://github.com/oovets/smokemon/security/advisories/new)

```
== supported versions ==

only main and the most recent tagged release receive security fixes. no lts branch.
  latest tag (0.12.x)   supported
  < 0.12                not supported
```

```
== reporting a vulnerability ==

do not file public github issues for security problems. report via one of:
- github security advisory (preferred):
  https://github.com/oovets/smokemon/security/advisories/new
- email stefan@weapply.se with the subject line "smokemon security:" and a clear
  description. encrypted reports welcome on request.

please include: affected version (git sha or tag), reproduction steps or proof-of-concept,
impact assessment (what an attacker gains), and a suggested remediation if you have one.

you get an acknowledgement within 3 working days. we aim to ship a fix within 30 days for
high-severity issues, longer for low-severity. credit goes in the release notes unless you
ask otherwise.
```

```
== known security-relevant surfaces ==

areas we already know are sensitive. reports still welcome - new attack angles are worth
flagging even where the trade-off is documented.

- hub ingest is http without tls (smokemon.hub). the shared secret travels in the
  x-smokemon-key header in cleartext. we assume the hub is exposed only over tailscale,
  wireguard, or another private l3 link. default bind is 0.0.0.0 for setup ease -
  production should override SMOKEMON_HUB_BIND to a specific interface.

- mtr with sudo -n on macos (smokemon.probes.mtr). install adds a nopasswd sudoers rule for
  mtr only. on linux this is avoided entirely via setcap cap_net_raw.

- subprocess arguments come from env-vars (SMOKEMON_TARGETS, SMOKEMON_MTR_TARGETS, etc.).
  argv lists are passed without shell=True, but an operator who sets a malicious env-var
  already has shell access to the box; treat env-var contents as trusted.

- no replay protection on ingest currently. a captured payload can be replayed and is
  silently absorbed by INSERT OR IGNORE on the UNIQUE(node, src_id) index, but a malicious
  node with the secret can backfill old data. on the roadmap.

- sqlite wal on a shared host is readable by anything with file-system access. no row-level
  encryption is performed.
```

```
== out of scope ==

- denial of service against the public internet by configuring smokemon to probe it
  heavily (you are responsible for what you measure).

- vulnerabilities in fping, mtr, curl, iperf3, iw, system_profiler, or the kernel.

- vulnerabilities in matplotlib, numpy, or plotext (report to those projects).
```
