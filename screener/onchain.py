"""On-chain reads: bonding-curve history (creator, creation-slot buyers), wallet profiles and funders."""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from solders.pubkey import Pubkey

from .clients import Helius
from .config import CURVE_MAX_PAGES, FIRST_SLOT_TX_CAP, HUB_SIG_THRESHOLD, PUMP_PROGRAM
from .db import DB

log = logging.getLogger("onchain")

SYSTEM_PROGRAM = "11111111111111111111111111111111"


def bonding_curve_pda(mint: str) -> str:
    pda, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                             Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def is_on_curve(address: str) -> bool:
    """False for program-derived addresses (pools, curves, vault authorities)."""
    try:
        return Pubkey.from_string(address).is_on_curve()
    except Exception:
        return False


def fee_payer(tx: dict) -> str | None:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    if not keys:
        return None
    k = keys[0]
    return k.get("pubkey") if isinstance(k, dict) else k


def token_deltas(tx: dict, mint: str) -> dict[str, int]:
    """Raw-unit change in `mint` balance per owner across this transaction."""
    meta = tx.get("meta") or {}
    acc: dict[str, int] = defaultdict(int)
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            if b.get("mint") != mint or not b.get("owner"):
                continue
            amt = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            acc[b["owner"]] += sign * amt
    return {k: v for k, v in acc.items() if v != 0}


def _all_instructions(tx: dict) -> list[dict]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    ixs = list(msg.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs.extend(inner.get("instructions") or [])
    return ixs


def find_funder(tx: dict, wallet: str) -> str | None:
    """Who sent `wallet` its first SOL in this transaction (system transfer or createAccount), else fee payer."""
    for ix in _all_instructions(tx):
        if ix.get("programId") != SYSTEM_PROGRAM:
            continue
        parsed = ix.get("parsed") or {}
        info = parsed.get("info") or {}
        t = parsed.get("type")
        if t in ("transfer", "transferWithSeed") and info.get("destination") == wallet:
            return info.get("source")
        if t in ("createAccount", "createAccountWithSeed") and info.get("newAccount") == wallet:
            return info.get("source")
    fp = fee_payer(tx)
    return fp if fp and fp != wallet else None


async def analyse_curve(h: Helius, mint: str, max_pages: int = CURVE_MAX_PAGES) -> dict:
    """Creator and creation-slot buyers, from the bonding-curve account's history.
    The curve is closed to trading after migration, so this history is fixed and safe to read at any time."""
    curve = bonding_curve_pda(mint)
    sigs: list[dict] = []
    before = None
    complete = False
    for _ in range(max_pages):
        page = await h.get_signatures(curve, limit=1000, before=before)
        sigs.extend(page)
        if len(page) < 1000:
            complete = True
            break
        before = page[-1]["signature"]
    out: dict = {"curve": curve, "n_sigs": len(sigs), "complete": complete, "creator": None,
                 "create_sig": None, "first_slot": None, "first_slot_buys": {}, "first_slot_txs_read": 0}
    if not sigs or not complete:
        return out
    oldest = sigs[-1]
    first_slot = oldest.get("slot")
    out["first_slot"] = first_slot
    # chronological order within the slot (API returns newest first)
    slot_sigs = [s for s in reversed(sigs) if s.get("slot") == first_slot and s.get("err") is None]
    buys: dict[str, int] = defaultdict(int)
    creator_fallback = None
    for s in slot_sigs[:FIRST_SLOT_TX_CAP]:
        tx = await h.get_transaction(s["signature"])
        if not tx:
            continue
        out["first_slot_txs_read"] += 1
        logs = (tx.get("meta") or {}).get("logMessages") or []
        if creator_fallback is None:
            creator_fallback = fee_payer(tx)
        if out["creator"] is None and any("Instruction: Create" in (l or "") for l in logs):
            out["creator"] = fee_payer(tx)
            out["create_sig"] = s["signature"]
        for owner, d in token_deltas(tx, mint).items():
            if d > 0 and is_on_curve(owner):
                buys[owner] += d
    if out["creator"] is None:
        out["creator"] = creator_fallback
    out["first_slot_buys"] = dict(buys)
    return out


async def wallet_profile(h: Helius, db: DB, wallet: str) -> dict:
    """Transaction count (capped at 1000), first-seen time and funder. Cached; history before now is fixed."""
    cached = db.get_wallet(wallet)
    if cached:
        return {"n_sigs": cached["n_sigs"], "oldest_ts": cached["oldest_ts"], "funder": cached["funder"],
                "profiled_ts": cached["profiled_ts"], "cached": True}
    sigs = await h.get_signatures(wallet, limit=1000)
    n = len(sigs)
    oldest_ts = sigs[-1].get("blockTime") if sigs else None
    funder = None
    if 0 < n < HUB_SIG_THRESHOLD:
        tx = await h.get_transaction(sigs[-1]["signature"])
        if tx:
            funder = find_funder(tx, wallet)
    db.put_wallet(wallet, n, oldest_ts, funder)
    return {"n_sigs": n, "oldest_ts": oldest_ts, "funder": funder, "profiled_ts": int(time.time()), "cached": False}


async def hub_sigs(h: Helius, db: DB, address: str) -> int:
    """Recent transaction count for a funder (capped at 1000). 1000 means exchange/hub-like."""
    cached = db.get_hub(address)
    if cached:
        return cached["n_sigs"]
    sigs = await h.get_signatures(address, limit=1000)
    db.put_hub(address, len(sigs))
    return len(sigs)
