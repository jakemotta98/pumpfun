"""Daily summary text and CSV export (features + outcomes) for analysis."""
from __future__ import annotations

import collections
import csv
import datetime as dt
import json
from pathlib import Path

from .db import DB
from .outcomes import measure


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


def daily_summary(db: DB, now: int, hours: int = 24) -> str:
    since = now - hours * 3600
    c = db.conn
    grads = c.execute("SELECT quote_mint, COUNT(*) n FROM migrations WHERE block_time>=? GROUP BY quote_mint",
                      (since,)).fetchall()
    total = sum(r["n"] for r in grads)
    by_quote = {("SOL" if (r["quote_mint"] or "").startswith("So111") else
                 "USDC" if r["quote_mint"] else "unknown"): r["n"] for r in grads}

    snaps = c.execute("SELECT label, status, late, kind, raw FROM snapshots WHERE taken_ts>=?", (since,)).fetchall()
    per_label = collections.defaultdict(collections.Counter)
    pre_reasons = collections.Counter()
    for s in snaps:
        per_label[s["label"]][s["status"]] += 1
        if s["late"]:
            per_label[s["label"]]["late"] += 1
        if s["kind"] == "deep":
            per_label[s["label"]]["deep"] += 1
    feats = c.execute("SELECT f.label, f.data FROM features f JOIN snapshots s ON s.id=f.snapshot_id "
                      "WHERE s.taken_ts>=?",
                      (since,)).fetchall()
    kill_counts = collections.Counter()
    survivors_4h = 0
    for f in feats:
        d = json.loads(f["data"])
        ps = d.get("prescreen") or {}
        if f["label"] == "1h":
            pre_reasons[ps.get("reason") or "pass"] += 1
        if f["label"] == "4h" and d.get("kills") is not None:
            for k in d["kills"]:
                kill_counts[k] += 1
            if not d["kills"]:
                survivors_4h += 1

    day = dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).strftime("%Y-%m-%d")
    calls = c.execute("SELECT method, n FROM rpc_calls WHERE day=? ORDER BY n DESC", (day,)).fetchall()
    rpc_total = sum(r["n"] for r in calls)
    errors = c.execute("SELECT COUNT(*) n FROM events WHERE ts>=? AND kind LIKE 'error%'", (since,)).fetchone()["n"]
    reconnects = c.execute("SELECT COUNT(*) n FROM events WHERE ts>=? AND kind='ws_reconnect'", (since,)).fetchone()["n"]

    lines = [f"Daily summary (last {hours}h)",
             f"Graduates: {total} ({', '.join(f'{k} {v}' for k, v in by_quote.items()) or 'none'})"]
    for label in ("15m", "1h", "4h", "24h"):
        cnt = per_label.get(label)
        if cnt:
            lines.append(f"Snapshots {label}: {cnt['done']} done, {cnt['deep']} deep, {cnt['late']} late, {cnt['error']} errors")
    if pre_reasons:
        lines.append("T+1h pre-screen: " + ", ".join(f"{k} {v}" for k, v in pre_reasons.most_common()))
    if kill_counts or survivors_4h:
        lines.append(f"T+4h survivors of all kill filters: {survivors_4h}")
        if kill_counts:
            lines.append("T+4h kills: " + ", ".join(f"{k} {v}" for k, v in kill_counts.most_common()))
    lines.append(f"Helius calls (UTC {day}): {_fmt(rpc_total)}, about {_fmt(rpc_total * 30)}/month at this rate")
    if calls:
        lines.append("Top methods: " + ", ".join(f"{r['method']} {_fmt(r['n'])}" for r in calls[:5]))
    lines.append("(Call counts, not credits: some methods cost more than 1 credit. Check the Helius dashboard.)")
    warn = c.execute("SELECT kind, COUNT(*) n FROM events WHERE ts>=? AND kind IN "
                     "('unparsed_migration','mint_heuristic','dex_outage','tx_unavailable') GROUP BY kind",
                     (since,)).fetchall()
    lines.append(f"Errors: {errors}. Websocket reconnects: {reconnects}")
    if warn:
        lines.append("Warnings: " + ", ".join(f"{r['kind']} {r['n']}" for r in warn))
    return "\n".join(lines)


def export_csv(db: DB, path: Path) -> int:
    rows = db.conn.execute(
        "SELECT f.mint, f.label, f.version, f.data, s.target_ts, s.taken_ts, s.late, m.block_time, m.symbol, "
        "m.quote_mint FROM features f JOIN snapshots s ON s.id=f.snapshot_id JOIN migrations m ON m.mint=f.mint "
        "ORDER BY m.block_time, f.label").fetchall()
    out_rows = []
    price_cache: dict[str, list] = {}
    for r in rows:
        d = json.loads(r["data"])
        flow = d.get("flow") or {}
        hm = d.get("holders") or {}
        trend = d.get("trend") or {}
        if r["mint"] not in price_cache:
            price_cache[r["mint"]] = [dict(p) for p in db.prices(r["mint"])]
        oc = measure(price_cache[r["mint"]], d.get("decision_ts") or r["taken_ts"], r["block_time"]) or {}
        out_rows.append({
            "mint": r["mint"], "symbol": r["symbol"], "label": r["label"], "feature_version": r["version"],
            "t0": r["block_time"], "target_ts": r["target_ts"], "taken_ts": r["taken_ts"], "late": r["late"],
            "quote_sol": int((r["quote_mint"] or "").startswith("So111")),
            "prescreen_pass": int(bool((d.get("prescreen") or {}).get("pass"))),
            "prescreen_reason": (d.get("prescreen") or {}).get("reason"),
            "deep": int(bool(d.get("deep"))),
            "kills": ";".join(d.get("kills") or []) if d.get("kills") is not None else "",
            "n_kills": len(d.get("kills") or []) if d.get("kills") is not None else "",
            "liq_usd": flow.get("liq_usd"), "vol_h1": flow.get("vol_h1"),
            "buys_per_k_vol": flow.get("buys_per_k_vol"), "buy_sell_ratio": flow.get("buy_sell_ratio"),
            "has_twitter": flow.get("has_twitter"), "has_telegram": flow.get("has_telegram"),
            "top10_pct": hm.get("top10_pct"), "max_cluster_pct": hm.get("max_cluster_pct"),
            "dev_pct": hm.get("dev_pct"), "sniper_pct": hm.get("sniper_pct"),
            "established_share": hm.get("established_share"), "funder_diversity": hm.get("funder_diversity"),
            "holder_count": hm.get("holder_count"), "top10_change_1h_4h": trend.get("top10_change"),
            "holder_growth_1h_4h": trend.get("holder_growth"), "creator": hm.get("creator"),
            **{k: oc.get(k) for k in ("entry_ts", "entry_lag_s", "entry_price_native", "mult_1h", "mult_24h",
                                      "mult_7d", "max_mult", "max_drawdown", "dead", "complete")},
        })
    if not out_rows:
        return 0
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    return len(out_rows)
