"""Take a snapshot: store the raw state of a token at a point in time. Features are computed later from this."""
from __future__ import annotations

import logging
import time

from .clients import Dexscreener, Helius, is_pumpswap, main_pair, sol_pumpswap_pair
from .config import MIN_LIQ_USD, TOP_N_HOLDERS, WSOL_MINT
from .db import DB
from .onchain import analyse_curve, hub_sigs, is_on_curve, wallet_profile

log = logging.getLogger("snapshot")


def prescreen(pairs: list[dict]) -> dict:
    if not pairs:
        return {"pass": False, "reason": "no_pair"}
    if not any(is_pumpswap(p) for p in pairs):
        return {"pass": False, "reason": "pair_not_indexed", "dex_ids": sorted({p.get("dexId") or "" for p in pairs})}
    p = sol_pumpswap_pair(pairs)
    if not p:
        return {"pass": False, "reason": "not_sol_quoted"}
    liq = (p.get("liquidity") or {}).get("usd") or 0
    res = {"dex_id": p.get("dexId"), "quote": WSOL_MINT, "liq_usd": liq, "pair": p.get("pairAddress")}
    if liq < MIN_LIQ_USD:
        return {**res, "pass": False, "reason": "low_liquidity"}
    return {**res, "pass": True, "reason": None}


async def deep_state(h: Helius, db: DB, mint: str) -> dict:
    """Core reads (supply, holders, curve) must succeed; optional sections record errors and carry on."""
    out: dict = {"errors": []}
    out["mint_account"] = await h.get_account_info(mint)
    out["supply"] = await h.get_token_supply(mint)

    largest = await h.get_token_largest_accounts(mint)
    out["largest"] = largest[:TOP_N_HOLDERS]
    accounts = await h.get_multiple_accounts([a["address"] for a in out["largest"]])
    if len(accounts) != len(out["largest"]):
        raise RuntimeError(f"getMultipleAccounts returned {len(accounts)} for {len(out['largest'])} accounts")
    holders = []
    for a, acct in zip(out["largest"], accounts):
        owner = None
        try:
            owner = acct["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            pass
        holders.append({"token_account": a["address"], "owner": owner, "amount": int(a.get("amount") or 0),
                        "owner_on_curve": bool(owner and is_on_curve(owner))})
    out["holders"] = holders

    # curve history (fixed after migration); cached per mint
    curve = db.get_curve(mint)
    if curve is None:
        curve = await analyse_curve(h, mint)
        db.put_curve(mint, curve.get("creator"), curve)
    out["curve"] = curve

    # wallet profiles for on-curve top holders and the creator
    profiles: dict[str, dict] = {}
    wallets = [x["owner"] for x in holders if x["owner_on_curve"]]
    creator = curve.get("creator")
    if creator:
        wallets.append(creator)
    for w in dict.fromkeys(wallets):
        try:
            profiles[w] = await wallet_profile(h, db, w)
        except Exception as e:
            out["errors"].append(f"profile {w}: {type(e).__name__}")
    out["profiles"] = profiles

    hubs: dict[str, int] = {}
    for p in profiles.values():
        f = p.get("funder")
        if f and f not in hubs:
            try:
                hubs[f] = await hub_sigs(h, db, f)
            except Exception as e:
                out["errors"].append(f"hub {f}: {type(e).__name__}")
    out["hub_sigs"] = hubs

    # current balances of the creator and the largest creation-slot buyers not already in the top list
    top_owners = {x["owner"] for x in holders if x["owner"]}
    check = []
    if creator and creator not in top_owners:
        check.append(creator)
    try:
        supply_raw = int((out["supply"] or {}).get("amount") or 0)
    except (TypeError, ValueError):
        supply_raw = 0
    # 10 largest creation-slot buyers; skip buys under 0.5% of supply (cannot move K7 on their own)
    fsb = sorted((curve.get("first_slot_buys") or {}).items(), key=lambda kv: kv[1], reverse=True)[:10]
    check.extend(o for o, amt in fsb if o not in top_owners and (not supply_raw or amt >= 0.005 * supply_raw))
    balances: dict[str, int] = {}
    for w in dict.fromkeys(check):
        try:
            accts = await h.get_token_accounts_by_owner(w, mint)
        except Exception as e:
            out["errors"].append(f"balance {w}: {type(e).__name__}")
            continue
        total = 0
        for a in accts:
            try:
                total += int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (TypeError, KeyError, ValueError):
                pass
        balances[w] = total
    out["balances_checked"] = list(balances)
    for x in holders:
        if x["owner"]:
            balances[x["owner"]] = balances.get(x["owner"], 0) + x["amount"]
    out["balances"] = balances

    # holder count via DAS (capped at 5 pages)
    try:
        count, cursor, pages, capped = 0, None, 0, False
        while True:
            res = await h.das_token_accounts(mint, limit=1000, cursor=cursor)
            accts = res.get("token_accounts") or []
            count += sum(1 for a in accts if int(a.get("amount") or 0) > 0)
            pages += 1
            cursor = res.get("cursor")
            if len(accts) < 1000 or not cursor:
                break
            if pages >= 5:
                capped = True
                break
        out["holder_count"] = {"count": count, "capped": capped}
    except Exception as e:
        out["errors"].append(f"das: {type(e).__name__}")
        out["holder_count"] = None
    return out


async def take_snapshot(h: Helius, dex: Dexscreener, db: DB, mint: str, label: str, deep_allowed: bool) -> tuple[dict, int | None]:
    slot = None
    try:
        slot = await h.get_slot()
    except Exception as e:  # slot is nice-to-have
        log.warning("get_slot failed: %s", e)
    started = int(time.time())
    all_pairs, failed = await dex.tokens_ex([mint])
    if failed:
        raise RuntimeError("dexscreener unavailable")  # never record an outage as 'no pair'
    pairs = [p for p in all_pairs if (p.get("baseToken") or {}).get("address") == mint]
    ps = prescreen(pairs)
    raw: dict = {"label": label, "started_ts": started, "slot": slot, "pairs": pairs, "prescreen": ps, "deep": None}
    p = sol_pumpswap_pair(pairs) or main_pair(pairs)
    if p:
        db.set_symbol(mint, (p.get("baseToken") or {}).get("symbol"), (p.get("quoteToken") or {}).get("address"))
    if deep_allowed and ps["pass"]:
        raw["deep"] = await deep_state(h, db, mint)
    raw["finished_ts"] = int(time.time())
    return raw, slot
