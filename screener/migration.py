"""Parse a Pump.fun migration transaction into (mint, quote mint, slot, block time)."""
from __future__ import annotations

from dataclasses import dataclass

from .config import PUMP_PROGRAM, PUMPSWAP_PROGRAM, USDC_MINT, WSOL_MINT

QUOTE_MINTS = {WSOL_MINT, USDC_MINT}


@dataclass
class Migration:
    mint: str
    quote_mint: str | None
    signature: str
    slot: int
    block_time: int
    method: str = "heuristic"


def logs_look_like_migration(logs: list[str] | None) -> bool:
    return any("Instruction: Migrate" in (line or "") for line in (logs or []))


def _program_ids(tx: dict) -> set[str]:
    ids: set[str] = set()
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ix in msg.get("instructions") or []:
        if ix.get("programId"):
            ids.add(ix["programId"])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            if ix.get("programId"):
                ids.add(ix["programId"])
    return ids


def curve_owned_mint(tx: dict | None) -> str | None:
    """The mint held by its own bonding-curve account in this transaction, if any."""
    from .onchain import bonding_curve_pda
    meta = (tx or {}).get("meta") or {}
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        m, o = b.get("mint"), b.get("owner")
        if m and o and m not in QUOTE_MINTS:
            try:
                if bonding_curve_pda(m) == o:
                    return m
            except Exception:
                pass
    return None


def parse_migration(tx: dict | None, signature: str) -> Migration | None:
    """Return the migration, or None if this transaction is not a successful Pump.fun -> PumpSwap migration."""
    if not tx or tx.get("blockTime") is None:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    if not logs_look_like_migration(meta.get("logMessages")):
        return None
    progs = _program_ids(tx)
    if PUMP_PROGRAM not in progs or PUMPSWAP_PROGRAM not in progs:
        return None

    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    pre_mints = {b.get("mint") for b in pre}
    post_mints = {b.get("mint") for b in post}
    all_mints = {m for m in pre_mints | post_mints if m}

    # Deterministic: the graduating mint is the one held by its own bonding-curve PDA before migration.
    from .onchain import bonding_curve_pda
    for b in pre:
        m = b.get("mint")
        if m and m not in QUOTE_MINTS and b.get("owner"):
            try:
                if bonding_curve_pda(m) == b["owner"]:
                    quote = USDC_MINT if USDC_MINT in all_mints else (WSOL_MINT if WSOL_MINT in all_mints else None)
                    return Migration(m, quote, signature, int(tx.get("slot") or 0), int(tx.get("blockTime") or 0),
                                     "curve_owner")
            except Exception:
                pass
    # Fallback heuristic: the graduating token existed before the migration; the LP mint is new.
    candidates = [m for m in pre_mints if m and m not in QUOTE_MINTS]
    if not candidates:
        candidates = [m for m in all_mints if m not in QUOTE_MINTS]
    if len(candidates) > 1:
        pump_suffix = [m for m in candidates if m.endswith("pump")]
        if len(pump_suffix) == 1:
            candidates = pump_suffix
        else:
            # largest token amount moved is the graduating token
            def moved(m: str) -> float:
                amt = 0.0
                for b in pre + post:
                    if b.get("mint") == m:
                        amt = max(amt, float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0))
                return amt
            candidates.sort(key=moved, reverse=True)
    if not candidates:
        return None
    quote = USDC_MINT if USDC_MINT in all_mints else (WSOL_MINT if WSOL_MINT in all_mints else None)
    return Migration(
        mint=candidates[0],
        quote_mint=quote,
        signature=signature,
        slot=int(tx.get("slot") or 0),
        block_time=int(tx.get("blockTime") or 0),
    )
