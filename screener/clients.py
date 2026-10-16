"""HTTP clients for Helius (Solana RPC + DAS) and Dexscreener, with rate limits, retries and call counting."""
from __future__ import annotations

import asyncio
import collections
import logging
import random
import re
import time
from typing import Any

import httpx

log = logging.getLogger("clients")


class RateLimiter:
    """Simple async token bucket."""

    def __init__(self, rate_per_s: float, burst: int | None = None):
        self.rate = rate_per_s
        self.capacity = burst or max(1, int(rate_per_s))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class RpcError(Exception):
    pass


class Helius:
    def __init__(self, rpc_url: str, rps: float, das_rps: float, http: httpx.AsyncClient | None = None):
        self.url = rpc_url
        self.http = http or httpx.AsyncClient(timeout=30)
        self.rl = RateLimiter(rps, burst=1)
        self.das_rl = RateLimiter(das_rps, burst=1)
        self.calls: collections.Counter[str] = collections.Counter()
        self._id = 0

    def take_counts(self) -> dict[str, int]:
        c = dict(self.calls)
        self.calls.clear()
        return c

    async def call(self, method: str, params: Any, das: bool = False, retries: int = 5) -> Any:
        delay = 1.0
        for attempt in range(retries):
            if das:  # DAS has its own cap and also counts against the overall request budget
                await self.das_rl.acquire()
            await self.rl.acquire()
            self._id += 1
            self.calls[method] += 1
            try:
                r = await self.http.post(self.url, json={"jsonrpc": "2.0", "id": self._id,
                                                         "method": method, "params": params})
            except httpx.HTTPError as e:
                log.warning("rpc %s transport error %s (attempt %d)", method, type(e).__name__, attempt + 1)
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("rpc %s http %d (attempt %d)", method, r.status_code, attempt + 1)
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30)
                continue
            if r.status_code != 200:
                raise RpcError(f"{method}: HTTP {r.status_code}")
            body = r.json()
            if "error" in body and body["error"]:
                err = body["error"]
                # -32429 / rate-limit style errors: back off and retry
                if isinstance(err, dict) and err.get("code") in (-32429, 429, -32005):
                    await asyncio.sleep(delay + random.random())
                    delay = min(delay * 2, 30)
                    continue
                raise RpcError(f"{method}: {err}")
            return body.get("result")
        raise RpcError(f"{method}: gave up after {retries} attempts")

    # --- standard RPC ---
    async def get_slot(self) -> int:
        return await self.call("getSlot", [{"commitment": "confirmed"}])

    max_tx_version = 1  # Solana transaction format; raised automatically if the network reports a newer one

    async def get_transaction(self, sig: str) -> dict | None:
        for _ in range(3):
            try:
                return await self.call("getTransaction", [sig, {
                    "encoding": "jsonParsed", "commitment": "confirmed",
                    "maxSupportedTransactionVersion": self.max_tx_version}])
            except RpcError as e:
                m = re.search(r'maxSupportedTransactionVersion\W+(\d+)', str(e))
                if "-32015" in str(e) and m and int(m.group(1)) > self.max_tx_version:
                    log.warning("raising maxSupportedTransactionVersion to %s", m.group(1))
                    Helius.max_tx_version = int(m.group(1))
                    continue
                raise
        return None

    async def get_block_accounts(self, slot: int) -> dict | None:
        """Whole block with per-transaction account keys and token balances, but no instructions (smaller)."""
        return await self.call("getBlock", [slot, {"encoding": "jsonParsed", "transactionDetails": "accounts",
                                                   "maxSupportedTransactionVersion": self.max_tx_version,
                                                   "rewards": False, "commitment": "confirmed"}])

    async def get_signatures(self, address: str, limit: int = 1000, before: str | None = None,
                             until: str | None = None) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def get_account_info(self, address: str) -> dict | None:
        res = await self.call("getAccountInfo", [address, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return (res or {}).get("value")

    async def get_multiple_accounts(self, addresses: list[str]) -> list[dict | None]:
        out: list[dict | None] = []
        for i in range(0, len(addresses), 100):
            res = await self.call("getMultipleAccounts", [addresses[i:i + 100],
                                                          {"encoding": "jsonParsed", "commitment": "confirmed"}])
            out.extend((res or {}).get("value") or [])
        return out

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        res = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return (res or {}).get("value") or []

    async def get_token_supply(self, mint: str) -> dict | None:
        res = await self.call("getTokenSupply", [mint, {"commitment": "confirmed"}])
        return (res or {}).get("value")

    async def get_token_accounts_by_owner(self, owner: str, mint: str) -> list[dict]:
        res = await self.call("getTokenAccountsByOwner", [owner, {"mint": mint},
                                                          {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return (res or {}).get("value") or []

    # --- DAS ---
    async def das_token_accounts(self, mint: str, limit: int = 1000, cursor: str | None = None) -> dict:
        params: dict[str, Any] = {"mint": mint, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self.call("getTokenAccounts", params, das=True) or {}


class Dexscreener:
    BASE = "https://api.dexscreener.com"

    def __init__(self, rps: float = 3.0, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(timeout=20)
        self.rl = RateLimiter(rps)
        self.calls = 0

    async def tokens(self, mints: list[str]) -> list[dict]:
        return (await self.tokens_ex(mints))[0]

    async def tokens_ex(self, mints: list[str]) -> tuple[list[dict], set[str]]:
        """All pairs for the given token addresses (GET /tokens/v1/solana/{a,b,c}, 30 per request), plus the
        set of mints whose request failed. Failures are returned per call, never kept as shared state."""
        out: list[dict] = []
        failed_mints: set[str] = set()
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            delay = 1.0
            got = False
            for attempt in range(4):
                await self.rl.acquire()
                self.calls += 1
                try:
                    r = await self.http.get(f"{self.BASE}/tokens/v1/solana/{','.join(chunk)}")
                    if r.status_code == 429 or r.status_code >= 500:
                        await asyncio.sleep(delay + random.random())
                        delay *= 2
                        continue
                    if r.status_code != 200:
                        log.warning("dexscreener HTTP %d for a chunk; skipping it", r.status_code)
                        break
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("dexscreener error %s", type(e).__name__)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                if isinstance(data, dict):
                    data = data.get("pairs") or []
                out.extend(p for p in data if isinstance(p, dict))
                got = True
                break
            if not got:
                failed_mints.update(chunk)
        return out, failed_mints


def pairs_by_mint(pairs: list[dict]) -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = collections.defaultdict(list)
    for p in pairs:
        base = (p.get("baseToken") or {}).get("address")
        if base:
            d[base].append(p)
    return d


def is_pumpswap(p: dict) -> bool:
    from .config import PUMPSWAP_DEX_IDS
    return (p.get("dexId") or "").lower() in PUMPSWAP_DEX_IDS


def main_pair(pairs: list[dict]) -> dict | None:
    """The deepest PumpSwap pair if present, else the deepest pair (for display only)."""
    if not pairs:
        return None
    pool = [p for p in pairs if is_pumpswap(p)] or pairs
    return max(pool, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))


def sol_pumpswap_pair(pairs: list[dict]) -> dict | None:
    """The deepest SOL-quoted PumpSwap pair. All prices and liquidity used in analysis come from this pair."""
    from .config import WSOL_MINT
    pool = [p for p in pairs if is_pumpswap(p) and (p.get("quoteToken") or {}).get("address") == WSOL_MINT]
    return max(pool, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0)) if pool else None


class RedactFilter(logging.Filter):
    """Keep API keys out of logs."""
    import re as _re
    PAT = _re.compile(r"(api-key=|/bot)[A-Za-z0-9:_\-]+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = self.PAT.sub(r"\1***", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True
