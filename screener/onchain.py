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
METAPLEX_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


def metadata_pda(mint: str) -> str:
    prog = Pubkey.from_string(METAPLEX_PROGRAM)
    pda, _ = Pubkey.find_program_address([b"metadata", bytes(prog), bytes(Pubkey.from_string(mint))], prog)
    return str(pda)


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
    t = tx.get("transaction") or {}
    keys = (t.get("message") or {}).get("accountKeys") or t.get("accountKeys") or []  # full or "accounts" mode
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


def _buys_from(txs: list[dict], mint: str) -> dict[str, int]:
    buys: dict[str, int] = defaultdict(int)
    for tx in txs:
        for owner, amt in token_deltas(tx, mint).items():
            if amt > 0 and is_on_curve(owner):
                buys[owner] += amt
    return dict(buys)


async def analyse_oldest_first(h: Helius, mint: str) -> dict:
    """Primary method: the bonding curve's oldest transactions, read oldest-first in one call (Helius
    getTransactionsForAddress). Creator = fee payer of the Create transaction; creation-slot buyers = every
    successful transaction in that slot (paged if the slot has more than 100)."""
    curve = bonding_curve_pda(mint)
    out: dict = {"method": "oldest_first", "curve": curve, "complete": False, "creator": None, "create_sig": None,
                 "first_slot": None, "first_slot_buys": {}, "first_slot_txs_read": 0, "n_sigs": None}
    res = await h.oldest_transactions(curve, limit=100)
    entries = res.get("data") or []
    if not entries:
        out["reason"] = "no transactions returned"
        return out
    first_slot = entries[0].get("slot")
    out["first_slot"] = first_slot
    txs = [e for e in entries if e.get("slot") == first_slot]
    if len(txs) == len(entries) and res.get("paginationToken"):  # slot fuller than one page
        token = res.get("paginationToken")
        for _ in range(10):
            more = await h.oldest_transactions(curve, limit=100, slot_eq=first_slot, token=token)
            txs.extend(more.get("data") or [])
            token = more.get("paginationToken")
            if not token:
                break
    for e in txs:
        logs = (e.get("meta") or {}).get("logMessages") or []
        if any("Instruction: Create" in (l or "") for l in logs):
            out["creator"] = fee_payer(e)
            out["create_sig"] = ((e.get("transaction") or {}).get("signatures") or [None])[0]
            break
    if out["creator"] is None:
        out["reason"] = "no Create transaction in the first slot"
        return out
    out["first_slot_txs_read"] = len(txs)
    out["first_slot_buys"] = _buys_from(txs, mint)
    out["complete"] = True
    return out


async def analyse_creation(h: Helius, mint: str) -> dict:
    """Creator and creation-slot buyers, independent of how busy the token was.
    The token's metadata account is written at creation and rarely afterwards, so its oldest transaction is the
    create transaction (verified by its 'Instruction: Create' log). Creation-slot buyers come from the whole block."""
    meta_addr = metadata_pda(mint)
    out: dict = {"method": "metadata+block", "metadata": meta_addr, "complete": False, "creator": None,
                 "create_sig": None, "first_slot": None, "first_slot_buys": {}, "first_slot_txs_read": 0,
                 "n_sigs": None}
    sigs = await h.get_signatures(meta_addr, limit=1000)
    out["n_sigs"] = len(sigs)
    if not sigs:
        out["reason"] = "metadata account has no transactions"
        return out
    if len(sigs) >= 1000:
        out["reason"] = "metadata account too busy"
        return out
    oldest = sigs[-1]
    tx = await h.get_transaction(oldest["signature"])
    logs = ((tx or {}).get("meta") or {}).get("logMessages") or []
    if not tx or not any("Instruction: Create" in (l or "") for l in logs):
        out["reason"] = "oldest metadata tx is not a Create"
        return out
    out["creator"] = fee_payer(tx)
    out["create_sig"] = oldest["signature"]
    out["first_slot"] = tx.get("slot") or oldest.get("slot")
    block = await h.get_block_accounts(out["first_slot"])
    if block is None:
        out["reason"] = "creation block unavailable"
        return out
    buys: dict[str, int] = defaultdict(int)
    for btx in block.get("transactions") or []:
        if ((btx.get("meta") or {}).get("err")) is not None:
            continue
        d = token_deltas(btx, mint)
        if not d:
            continue
        out["first_slot_txs_read"] += 1
        for owner, amt in d.items():
            if amt > 0 and is_on_curve(owner):
                buys[owner] += amt
    out["first_slot_buys"] = dict(buys)
    out["complete"] = out["first_slot_txs_read"] > 0
    if not out["complete"]:
        out["reason"] = "no token movements found in the creation block"
    return out


async def analyse_curve(h: Helius, mint: str, max_pages: int = CURVE_MAX_PAGES) -> dict:
    """Creator and creation-slot buyers. Primary method: metadata account + creation block (cheap, works for
    busy tokens). Fallback: the bonding-curve account's own history (fails past max_pages x 1000 txs)."""
    failures = []
    for name, fn in (("oldest_first", analyse_oldest_first), ("metadata+block", analyse_creation)):
        try:
            res = await fn(h, mint)
            if res.get("complete") and res.get("creator"):
                if failures:
                    res["earlier_failures"] = failures
                return res
            failures.append(f"{name}: {res.get('reason') or 'incomplete'}")
        except Exception as e:
            log.warning("%s creation lookup failed for %s: %s", name, mint, type(e).__name__)
            failures.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    fb = await analyse_curve_history(h, mint, max_pages)
    fb["method"] = "curve_history"
    fb["earlier_failures"] = failures
    return fb


async def analyse_curve_history(h: Helius, mint: str, max_pages: int = CURVE_MAX_PAGES) -> dict:
    """Fallback: creator and creation-slot buyers from the bonding-curve account's history.
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
