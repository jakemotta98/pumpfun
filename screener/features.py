"""Kill filters and features, computed ONLY from stored snapshot dumps (no live calls)."""
from __future__ import annotations

from collections import defaultdict

from .clients import sol_pumpswap_pair
from .config import (CLUSTER_MAX_PCT, DEV_MAX_PCT, ESTABLISHED_AGE_DAYS, FEATURE_VERSION, HUB_SIG_THRESHOLD,
                     INCINERATOR, MIN_LIQ_USD, SNIPER_MAX_PCT, TOKEN_2022_PROGRAM, TOP10_MAX_PCT, WSOL_MINT)

RISKY_EXTENSIONS = {"transferFeeConfig", "transferHook", "permanentDelegate"}


def _supply_raw(deep: dict) -> int:
    try:
        return int(deep["supply"]["amount"])
    except (TypeError, KeyError, ValueError):
        return 0


def _pct(amount: int, supply: int) -> float:
    return 100.0 * amount / supply if supply else 0.0


def mint_risks(deep: dict) -> dict:
    acct = deep.get("mint_account") or {}
    owner_prog = acct.get("owner")
    info = (((acct.get("data") or {}).get("parsed") or {}).get("info")) or {}
    exts = {e.get("extension") for e in info.get("extensions") or [] if isinstance(e, dict)}
    default_frozen = any(
        e.get("extension") == "defaultAccountState" and (e.get("state") or {}).get("accountState") == "frozen"
        for e in info.get("extensions") or [] if isinstance(e, dict)
    )
    return {
        "mint_authority": info.get("mintAuthority"),
        "freeze_authority": info.get("freezeAuthority"),
        "token_2022": owner_prog == TOKEN_2022_PROGRAM,
        "risky_extensions": sorted((exts & RISKY_EXTENSIONS) | ({"defaultFrozen"} if default_frozen else set())),
    }


def holder_metrics(deep: dict, snapshot_ts: int | None = None) -> dict:
    supply = _supply_raw(deep)
    holders = [x for x in deep.get("holders") or [] if x.get("owner")]
    retail = [x for x in holders if x.get("owner_on_curve") and x["owner"] != INCINERATOR]
    # aggregate by owner (one owner may have several token accounts)
    by_owner: dict[str, int] = defaultdict(int)
    for x in retail:
        by_owner[x["owner"]] += x["amount"]
    ranked = sorted(by_owner.items(), key=lambda kv: kv[1], reverse=True)
    top10 = sum(a for _, a in ranked[:10])

    profiles = deep.get("profiles") or {}
    hubs = deep.get("hub_sigs") or {}
    creator = (deep.get("curve") or {}).get("creator")

    def funder_of(w: str) -> str | None:
        f = (profiles.get(w) or {}).get("funder")
        if not f or hubs.get(f, 0) >= HUB_SIG_THRESHOLD:
            return None
        return f

    clusters: dict[str, list[str]] = defaultdict(list)
    for w, _ in ranked:
        f = funder_of(w)
        if f:
            clusters[f].append(w)
    cluster_pcts = {f: _pct(sum(by_owner[w] for w in ws), supply) for f, ws in clusters.items() if len(ws) >= 2}
    max_cluster = max(cluster_pcts.values(), default=0.0)

    balances = deep.get("balances") or {}
    # K5 per pre-registration: creator plus top-20 wallets whose first SOL came from the creator
    dev_wallets = set()
    if creator:
        dev_wallets.add(creator)
        for w, _ in ranked:
            if (profiles.get(w) or {}).get("funder") == creator:
                dev_wallets.add(w)
    dev_amt = sum(balances.get(w, by_owner.get(w, 0)) for w in dev_wallets)

    fsb = (deep.get("curve") or {}).get("first_slot_buys") or {}
    sniper_amt = sum(balances.get(w, 0) for w in fsb if w in balances)

    known = [w for w, _ in ranked if w in profiles]
    established = 0
    for w in known:
        p = profiles[w]
        age_days = None
        ref_ts = snapshot_ts or p.get("profiled_ts")
        if p.get("oldest_ts") and ref_ts:
            age_days = (ref_ts - p["oldest_ts"]) / 86400
        if (p.get("n_sigs") or 0) >= HUB_SIG_THRESHOLD or (age_days is not None and age_days >= ESTABLISHED_AGE_DAYS):
            established += 1
    funders = [funder_of(w) for w in known]
    known_f = [f for f in funders if f]
    return {
        "supply_raw": supply,
        "top10_pct": _pct(top10, supply),
        "n_retail_top": len(ranked),
        "max_cluster_pct": max_cluster,
        "n_clusters": len(cluster_pcts),
        "dev_pct": _pct(dev_amt, supply),
        "n_dev_wallets": len(dev_wallets),
        "sniper_pct": _pct(sniper_amt, supply),
        "n_first_slot_buyers": len(fsb),
        "established_share": established / len(known) if known else None,
        "funder_diversity": len(set(known_f)) / len(known_f) if known_f else None,
        "holder_count": (deep.get("holder_count") or {}).get("count"),
        "holder_count_capped": (deep.get("holder_count") or {}).get("capped"),
        "creator": creator,
        "curve_complete": (deep.get("curve") or {}).get("complete"),
        "curve_n_sigs": (deep.get("curve") or {}).get("n_sigs"),
        "deep_errors": len(deep.get("errors") or []),
    }


def flow_metrics(pairs: list[dict]) -> dict:
    p = sol_pumpswap_pair(pairs) or {}
    tx = (p.get("txns") or {}).get("h1") or {}
    vol = (p.get("volume") or {}).get("h1") or 0
    buys, sells = tx.get("buys") or 0, tx.get("sells") or 0
    info = p.get("info") or {}
    socials = {(s.get("type") or s.get("platform") or "").lower() for s in info.get("socials") or []}
    return {
        "price_native": float(p["priceNative"]) if p.get("priceNative") else None,
        "price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
        "liq_usd": (p.get("liquidity") or {}).get("usd"),
        "quote": (p.get("quoteToken") or {}).get("address"),
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "vol_h1": vol,
        "buys_h1": buys,
        "sells_h1": sells,
        "buys_per_k_vol": (buys / (vol / 1000)) if vol else None,
        "buy_sell_ratio": (buys / sells) if sells else None,
        "has_twitter": bool(socials & {"twitter", "x"}),
        "has_telegram": "telegram" in socials,
        "has_website": bool(info.get("websites")),
    }


def kills(flow: dict, hm: dict | None, risks: dict | None) -> list[str]:
    k = []
    if flow.get("quote") != WSOL_MINT:
        k.append("K1_not_sol")
    if hm is not None and (hm.get("curve_complete") is not True or not hm.get("creator")):
        k.append("K0_curve_unknown")  # creator/creation-slot data missing: excluded, never passed by default
    if risks:
        if risks["mint_authority"] or risks["freeze_authority"]:
            k.append("K2_authority")
        if risks["token_2022"] and risks["risky_extensions"]:
            k.append("K3_token2022")
    if hm:
        if hm["max_cluster_pct"] > CLUSTER_MAX_PCT:
            k.append("K4_cluster")
        if hm["dev_pct"] > DEV_MAX_PCT:
            k.append("K5_dev")
        if hm["top10_pct"] > TOP10_MAX_PCT:
            k.append("K6_top10")
        if hm["sniper_pct"] > SNIPER_MAX_PCT:
            k.append("K7_snipers")
    if (flow.get("liq_usd") or 0) < MIN_LIQ_USD:
        k.append("K8_liquidity")
    return k


def compute(raw: dict) -> dict:
    """Features for one snapshot dump."""
    pairs = raw.get("pairs") or []
    flow = flow_metrics(pairs)
    deep = raw.get("deep")
    hm = holder_metrics(deep, raw.get("started_ts")) if deep else None
    risks = mint_risks(deep) if deep else None
    out = {
        "version": FEATURE_VERSION,
        "label": raw.get("label"),
        "decision_ts": raw.get("finished_ts"),
        "deep": deep is not None,
        "prescreen": raw.get("prescreen"),
        "flow": flow,
        "holders": hm,
        "mint_risks": risks,
    }
    out["kills"] = kills(flow, hm, risks) if deep else None
    return out


def add_trend(features_by_label: dict[str, dict]) -> None:
    """F3: change in top-10 share and holder count from T+1h to T+4h (both stored snapshots)."""
    a = (features_by_label.get("1h") or {}).get("holders")
    b = (features_by_label.get("4h") or {}).get("holders")
    if a and b and "4h" in features_by_label:
        features_by_label["4h"]["trend"] = {
            "top10_change": b["top10_pct"] - a["top10_pct"],
            "holder_growth": (b["holder_count"] - a["holder_count"])
            if a.get("holder_count") is not None and b.get("holder_count") is not None else None,
        }
