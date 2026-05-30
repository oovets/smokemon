"""Read-only query layer behind the hub's GET endpoints: a Prometheus/OpenMetrics
exposition (S2) and a small JSON API with a fleet ranking and a node×hour heatmap
(S3). Pure stdlib, derives everything from the hub DB via direct SQL + the shared
analysis engine. Split out from hub.py so it can be unit-tested without a socket.

Latest-value queries lean on SQLite's documented bare-column behaviour: with a
MAX(ts) aggregate and GROUP BY node, the other selected columns come from the row
that holds that max ts - i.e. the most recent sample per node."""

import sqlite3
import time

from . import analyze, config, query


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def nodes(conn) -> list[str]:
    """Distinct node names known to the hub DB."""
    seen: set[str] = set()
    for t in ("host_samples", "ping_runs", "net_samples"):
        for (n,) in _rows(conn, f"SELECT DISTINCT node FROM {t}"):
            if n:
                seen.add(n)
    return sorted(seen)


def latest_metrics(conn) -> dict[str, dict]:
    """{node: {cpu, mem, temp, ts, targets:{name:{rtt,loss}}}} most-recent values."""
    out: dict[str, dict] = {}
    for node, cpu, mem, temp, ts in _rows(
            conn, "SELECT node, cpu_pct, mem_used_pct, temp_c, MAX(ts) FROM host_samples GROUP BY node"):
        out.setdefault(node, {})["cpu"] = cpu
        out[node]["mem"] = mem
        out[node]["temp"] = temp
        out[node]["ts"] = ts
    for node, target, rtt, loss, ts in _rows(
            conn, "SELECT node, target, rtt_median, loss_pct, MAX(ts) FROM ping_runs GROUP BY node, target"):
        d = out.setdefault(node, {}).setdefault("targets", {})
        d[target] = {"rtt_ms": rtt, "loss_pct": loss, "ts": ts}
    return out


# ---------- S2: Prometheus / OpenMetrics ----------


def _metric(name, help_text, typ, samples) -> list[str]:
    """One metric block (HELP/TYPE + samples). samples: [(labels_str, value)]."""
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}"]
    for labels, value in samples:
        if value is None:
            continue
        out.append(f"{name}{{{labels}}} {value}")
    return out


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def prometheus(conn) -> str:
    """OpenMetrics text exposition of the latest gauges per node. Plugs straight into a
    Prometheus scrape -> Grafana / Alertmanager."""
    latest = latest_metrics(conn)
    rtt, loss, cpu, mem, temp = [], [], [], [], []
    for node, d in sorted(latest.items()):
        nl = f'node="{_esc(node)}"'
        if "cpu" in d:
            cpu.append((nl, d.get("cpu")))
            mem.append((nl, d.get("mem")))
            temp.append((nl, d.get("temp")))
        for target, td in sorted(d.get("targets", {}).items()):
            tl = f'{nl},target="{_esc(target)}"'
            rtt.append((tl, td.get("rtt_ms")))
            loss.append((tl, td.get("loss_pct")))
    lines: list[str] = []
    lines += _metric("smokemon_ping_rtt_ms", "Median ping RTT (ms), latest sample", "gauge", rtt)
    lines += _metric("smokemon_ping_loss_pct", "Ping loss percent, latest sample", "gauge", loss)
    lines += _metric("smokemon_cpu_pct", "CPU utilisation percent, latest sample", "gauge", cpu)
    lines += _metric("smokemon_mem_pct", "Memory used percent, latest sample", "gauge", mem)
    lines += _metric("smokemon_temp_c", "Temperature (C), latest sample", "gauge", temp)
    return "\n".join(lines) + "\n"


# ---------- S3: fleet ranking + heatmap ----------


def fleet(conn, hours: float = 24.0, until: float | None = None) -> list[dict]:
    """Per-node health over the last `hours`, ranked worst-first: internet uptime %,
    median RTT, incident count. Uses the same detector as `smoke incidents`."""
    until = time.time() if until is None else until
    since = until - hours * 3600
    out = []
    for node in nodes(conn):
        ping = query.load_ping_agg(conn, since, until, None, node)
        http = query.load_http(conn, since, until, node)
        incidents = analyze.detect_incidents(ping, http)
        cls = analyze.classify_targets(ping)
        inet = cls["internet"] or list(ping)
        up_pct, rtt_med = None, None
        if inet:
            losses, meds = [], []
            for name in inet:
                losses += ping[name]["loss"]
                meds += [m for m in ping[name]["med"] if m is not None]
            if losses:
                up_pct = round(100.0 * sum(1 for x in losses if (x or 0) < 100.0) / len(losses), 2)
            rtt_med = round(analyze._median(meds), 1) if meds else None
        hard = analyze.merge_spans([(i["start"], i["end"]) for i in incidents
                                    if i["klass"] in ("isp-outage", "link-down")])
        out.append({
            "node": node, "uptime_pct": up_pct, "rtt_ms": rtt_med,
            "incidents": len(incidents),
            "downtime_s": round(sum(e - s for s, e in hard), 1),
        })
    # Worst first: least uptime, then most downtime, then most incidents.
    out.sort(key=lambda r: (r["uptime_pct"] if r["uptime_pct"] is not None else 100.0,
                            -r["downtime_s"], -r["incidents"]))
    return out


def heatmap(conn, metric: str = "loss", hours: float = 24.0, until: float | None = None) -> dict:
    """node × hour grid for 'loss' (max loss%) or 'rtt' (median RTT). Returns
    {metric, hours:[epoch...], nodes:{node:[val per hour]}}."""
    until = time.time() if until is None else until
    since = until - hours * 3600
    n_buckets = int(hours)
    hour0 = since - (since % 3600)
    col = "loss_pct" if metric == "loss" else "rtt_median"
    agg = "MAX" if metric == "loss" else "AVG"
    grid: dict[str, list] = {}
    rows = _rows(conn,
                 f"SELECT node, CAST((ts - ?) / 3600 AS INT) hr, {agg}({col}) "
                 "FROM ping_runs WHERE ts BETWEEN ? AND ? GROUP BY node, hr",
                 (hour0, since, until))
    for node, hr, val in rows:
        if node is None or hr is None:
            continue
        series = grid.setdefault(node, [None] * (n_buckets + 1))
        if 0 <= hr < len(series):
            series[hr] = round(val, 1) if val is not None else None
    return {"metric": metric, "hours": [hour0 + i * 3600 for i in range(n_buckets + 1)], "nodes": grid}


# ---------- S6: live fleet dashboard ----------

# At-a-glance state thresholds. Deliberately latest-sample based (no incident
# detection) so the endpoint stays a handful of GROUP BY queries even at 150 nodes;
# the deep view is `smoke incidents` / /api/fleet.
_WARN_RTT_MS = 250.0          # reachable but high internet RTT -> warn
_WARN_LOSS_PCT = 1.0          # sustained loss at/above this -> warn
FLEET_STALE_AFTER_S = 300.0   # no fresh sample within this -> stale/offline
_STATE_ORDER = {"down": 0, "stale": 1, "warn": 2, "healthy": 3}


def _wan_target(targets: dict) -> str | None:
    """WAN-representative target from a latest_metrics targets dict: prefer those
    classified 'internet' (largest RTT among them), else the largest-RTT target."""
    if not targets:
        return None
    inet = [n for n in targets if analyze.classify_target(n) == "internet"] or list(targets)
    return max(inet, key=lambda n: targets[n].get("rtt_ms") or 0.0)


def _node_state(age_s, loss, rtt, temp, stale_after_s) -> str:
    if age_s is None or age_s > stale_after_s:
        return "stale"
    if loss is not None and loss >= 100.0:
        return "down"
    if ((loss is not None and loss >= _WARN_LOSS_PCT)
            or (rtt is not None and rtt > _WARN_RTT_MS)
            or (temp is not None and temp >= config.THROTTLE_TEMP_C - 5.0)):
        return "warn"
    return "healthy"


def fleet_status(conn, stale_after_s: float = FLEET_STALE_AFTER_S, now: float | None = None) -> dict:
    """Fast per-node status for the live dashboard: one latest_metrics() pass (a few
    GROUP BY queries, no incident detection) -> derived state + key gauges, sorted
    worst-first. Returns {now, stale_after_s, counts:{state:n}, nodes:[{node, state,
    rtt_ms, loss_pct, cpu, temp, age_s}]}."""
    now = time.time() if now is None else now
    latest = latest_metrics(conn)
    counts = {"healthy": 0, "warn": 0, "down": 0, "stale": 0}
    out = []
    for node, d in latest.items():
        targets = d.get("targets", {})
        tgt = _wan_target(targets)
        rtt = loss = None
        tstamps = [d.get("ts")]
        if tgt:
            rtt = targets[tgt].get("rtt_ms")
            loss = targets[tgt].get("loss_pct")
            tstamps.append(targets[tgt].get("ts"))
        last_ts = max([t for t in tstamps if t is not None], default=None)
        age = (now - last_ts) if last_ts is not None else None
        state = _node_state(age, loss, rtt, d.get("temp"), stale_after_s)
        counts[state] += 1
        out.append({"node": node, "state": state, "rtt_ms": rtt, "loss_pct": loss,
                    "cpu": d.get("cpu"), "temp": d.get("temp"),
                    "age_s": round(age) if age is not None else None})
    out.sort(key=lambda r: (_STATE_ORDER[r["state"]],
                            -(r["loss_pct"] or 0.0), -(r["rtt_ms"] or 0.0), r["node"]))
    return {"now": now, "stale_after_s": stale_after_s, "counts": counts, "nodes": out}


def dashboard_html() -> str:
    """Self-contained fleet dashboard (no external assets). Polls /api/fleet-status and
    renders an ultra-dense, worst-first, colour-coded one-line-per-node grid. Refresh
    interval via ?refresh=SEC (default 5)."""
    return _DASHBOARD_HTML


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>smokemon fleet</title>
<style>
 :root{--bg:#0b0e14;--fg:#c9d1d9;--mut:#6b7280;--card:#11151c;--line:#1c222c;
       --ok:#2ea043;--warn:#d29922;--down:#f85149;--stale:#484f58}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:13px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
        padding:10px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 h1{font-size:14px;margin:0;letter-spacing:.5px;font-weight:500}
 .pills{display:flex;gap:8px}
 .pill{padding:2px 9px;border-radius:10px;font-size:12px;display:flex;gap:6px;align-items:center}
 .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
 .s-healthy{background:var(--ok)}.s-warn{background:var(--warn)}
 .s-down{background:var(--down)}.s-stale{background:var(--stale)}
 .pill.healthy{background:#0f2417;color:#7ee2a8}.pill.warn{background:#241d0f;color:#e9c46a}
 .pill.down{background:#2a1314;color:#ff7b72}.pill.stale{background:#1a1d22;color:#8b949e}
 input{background:var(--card);border:1px solid var(--line);color:var(--fg);
       padding:5px 8px;border-radius:6px;font:inherit;min-width:160px}
 .meta{color:var(--mut);font-size:12px;margin-left:auto}
 #grid{padding:8px 12px;column-width:230px;column-gap:14px}
 .node{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:5px;
       break-inside:avoid;border-left:3px solid var(--stale)}
 .node:hover{background:var(--card)}
 .node.healthy{border-left-color:var(--ok)}.node.warn{border-left-color:var(--warn)}
 .node.down{border-left-color:var(--down)}.node.stale{border-left-color:var(--stale);color:var(--mut)}
 .name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .m{color:var(--mut);font-size:12px;flex:0 0 auto;min-width:44px;text-align:right}
 .m.bad{color:#ff7b72}
 #err{color:#ff7b72;padding:0 14px}
</style></head>
<body>
<header>
 <h1>smokemon <b>FLEET</b></h1>
 <div class="pills" id="pills"></div>
 <input id="q" placeholder="filter nodes…" autocomplete="off">
 <span class="meta" id="meta">connecting…</span>
</header>
<div id="err"></div>
<div id="grid"></div>
<script>
const params=new URLSearchParams(location.search);
const REFRESH=Math.max(1,parseFloat(params.get("refresh"))||5)*1000;
const q=document.getElementById("q"),grid=document.getElementById("grid"),
 pills=document.getElementById("pills"),meta=document.getElementById("meta"),
 err=document.getElementById("err");
let last={nodes:[],counts:{}};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtRtt=r=>r==null?"--":Math.round(r)+"ms";
const fmtLoss=l=>l==null?"":"l"+Math.round(l)+"%";
function fmtAge(a){if(a==null)return"?";if(a<90)return a+"s";if(a<5400)return Math.round(a/60)+"m";return Math.round(a/3600)+"h";}
function render(){
 const term=q.value.trim().toLowerCase();
 const nodes=last.nodes.filter(n=>!term||n.node.toLowerCase().includes(term));
 grid.innerHTML=nodes.map(n=>{
  const lossBad=n.loss_pct!=null&&n.loss_pct>0?" bad":"";
  const right=n.state==="stale"
   ?`<span class="m">${fmtAge(n.age_s)} ago</span>`
   :`<span class="m">${fmtRtt(n.rtt_ms)}</span><span class="m${lossBad}">${fmtLoss(n.loss_pct)}</span>`;
  return `<div class="node ${n.state}" title="${esc(n.node)} · cpu ${n.cpu??"?"}% · ${n.temp??"?"}°C · ${fmtAge(n.age_s)} ago">`
   +`<span class="dot s-${n.state}"></span><span class="name">${esc(n.node)}</span>${right}</div>`;
 }).join("");
 const c=last.counts||{};
 pills.innerHTML=[["healthy"],["warn"],["down"],["stale"]]
  .map(([k])=>`<span class="pill ${k}"><span class="dot s-${k}"></span>${c[k]||0}</span>`).join("");
}
async function tick(){
 try{
  const r=await fetch("/api/fleet-status",{cache:"no-store"});
  if(!r.ok)throw new Error("HTTP "+r.status);
  last=await r.json();err.textContent="";
  meta.textContent=`${last.nodes.length} nodes · updated ${new Date().toLocaleTimeString()} · refresh ${REFRESH/1000}s`;
  render();
 }catch(e){err.textContent="fetch error: "+e.message;}
}
q.addEventListener("input",render);
tick();setInterval(tick,REFRESH);
</script>
</body></html>
"""
