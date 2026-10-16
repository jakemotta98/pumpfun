"""The long-running service: listener, backfill, snapshot worker, price sampler, reports, Telegram commands."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import websockets

from . import features as F
from .clients import Dexscreener, Helius, pairs_by_mint, sol_pumpswap_pair
from .config import (DEAD_LIQ_USD, DEAD_NO_DATA_HOURS, DEEP_LABELS, FEATURE_VERSION, MIGRATION_ACCOUNT,
                     SAMPLE_DAYS, SNAPSHOTS, Settings)
from .db import DB, unpack
from .migration import curve_owned_mint, logs_look_like_migration, parse_migration
from .report import daily_summary
from .snapshot import prescreen, take_snapshot
from .telegram import Telegram

log = logging.getLogger("app")
ROOT = Path(__file__).resolve().parent.parent
MAX_LATE_S = 6 * 3600
STARTUP_LOOKBACK_S = 2 * 3600
TX_RETRY_DELAY_S = 30
TX_MAX_TRIES = 5


def prereg_hash() -> str:
    p = ROOT / "PREREGISTRATION.md"
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "missing"


class App:
    def __init__(self, s: Settings):
        self.s = s
        self.db = DB(s.db_path)
        self.http = httpx.AsyncClient(timeout=30)
        self.h = Helius(s.rpc_url, s.rpc_rps, s.das_rps, self.http)
        self.dex = Dexscreener(http=self.http)
        self.tg = Telegram(s.telegram_token, s.telegram_chat_id)
        self.deep_sem = asyncio.Semaphore(s.deep_concurrency)
        self.running: set[int] = set()
        self.tasks: set[asyncio.Task] = set()
        self.last_migration_ts = time.time()
        self.ws_connected = False
        self.alerted_quiet = False

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    # ---------- detection ----------
    def _enqueue(self, sig: str, source: str) -> None:
        if not self.db.has_signature(sig):
            self.db.queue_sig(sig, source)

    async def listener(self) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.s.ws_url, ping_interval=20, ping_timeout=20,
                                              max_size=2**22) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [MIGRATION_ACCOUNT]},
                                                         {"commitment": "confirmed"}]}))
                    self.ws_connected = True
                    log.info("websocket subscribed")
                    backoff = 1
                    self.spawn(self.safe_backfill("reconnect"))
                    async for msg in ws:
                        data = json.loads(msg)
                        val = (((data.get("params") or {}).get("result") or {}).get("value")) or {}
                        if not val.get("signature") or val.get("err") is not None:
                            continue
                        if logs_look_like_migration(val.get("logs")):
                            self._enqueue(val["signature"], "ws")
                self.ws_connected = False
                self.db.event("ws_reconnect", "closed")
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ws_connected = False
                self.db.event("ws_reconnect", type(e).__name__)
                log.warning("websocket dropped (%s); reconnecting in %ss", type(e).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def backfill_once(self, why: str) -> None:
        """Queue every signature on the migration account since the last one seen. The queue is durable, so a
        crash after this point loses nothing. On the very first start, only the last 2 hours are queued."""
        until = self.db.get_meta("last_sig")
        collected: list[dict] = []
        before = None
        cutoff = time.time() - STARTUP_LOOKBACK_S
        for i in range(10):
            page = await self.h.get_signatures(MIGRATION_ACCOUNT, limit=1000, before=before, until=until)
            if until is None:
                page = [p for p in page if (p.get("blockTime") or 0) >= cutoff]
            collected.extend(page)
            if len(page) < 1000:
                break
            if i == 9:
                self.db.event("backfill_truncated", f"gap larger than 10,000 signatures ({why})")
            before = page[-1]["signature"]
        queued = 0
        for sg in reversed(collected):
            if sg.get("err") is None and not self.db.has_signature(sg["signature"]):
                queued += int(self.db.queue_sig(sg["signature"], f"backfill:{why}"))
        if collected:
            self.db.set_meta("last_sig", collected[0]["signature"])
        if queued:
            log.info("backfill (%s) queued %d signatures", why, queued)

    async def safe_backfill(self, why: str) -> None:
        try:
            await self.backfill_once(why)
        except Exception as e:
            self.db.event("error_backfill", f"{why}: {type(e).__name__}: {e}"[:500])
            log.warning("backfill (%s) failed: %s", why, type(e).__name__)

    async def backfill_loop(self) -> None:
        while True:
            await self.safe_backfill("periodic")
            await asyncio.sleep(self.s.backfill_interval_s)

    async def process_sig(self, sig: str, source: str = "ws") -> None:
        tx = await self.h.get_transaction(sig)
        if tx is None or tx.get("blockTime") is None:
            tries = self.db.retry_sig(sig, TX_RETRY_DELAY_S)
            if tries >= TX_MAX_TRIES:
                self.db.event("tx_unavailable", sig)
                self.db.done_sig(sig, "tx_unavailable")
            return
        m = parse_migration(tx, sig)
        if m is None:
            logs = (tx.get("meta") or {}).get("logMessages")
            if logs_look_like_migration(logs) and (tx.get("meta") or {}).get("err") is None:
                self.db.event("unparsed_migration", f"{sig} {curve_owned_mint(tx) or '-'}")
            self.db.done_sig(sig, "not_migration")
            return
        if m.method != "curve_owner":
            self.db.event("mint_heuristic", f"{sig} {m.mint}")
        if self.db.add_migration(m.mint, m.signature, m.slot, m.block_time, m.quote_mint, source, SNAPSHOTS):
            self.last_migration_ts = time.time()
            self.alerted_quiet = False
            log.info("migration %s (lag %ss)", m.mint, int(time.time()) - m.block_time)
        self.db.done_sig(sig, "migration")

    async def processor(self) -> None:
        while True:
            rows = self.db.next_sigs(int(time.time()))
            for r in rows:
                try:
                    await self.process_sig(r["signature"], r["source"])
                except Exception as e:
                    self.db.event("error_process", f"{r['signature']}: {type(e).__name__}")
                    tries = self.db.retry_sig(r["signature"], TX_RETRY_DELAY_S)
                    if tries >= TX_MAX_TRIES:
                        self.db.done_sig(r["signature"], "error")
            if not rows:
                await asyncio.sleep(2)

    # ---------- snapshots ----------
    def _deep_allowed(self, mint: str, label: str) -> bool:
        if label not in DEEP_LABELS:
            return False
        if label == "24h":  # 24h deep only for tokens that passed every kill filter at 4h (credit budget)
            f4 = self.db.features_data(mint, "4h")
            return bool(f4 and f4.get("kills") == [])
        return True

    async def run_snapshot(self, row) -> None:
        snap_id, mint, label, target = row["id"], row["mint"], row["label"], row["target_ts"]
        tol = SNAPSHOTS[label][1]
        try:
            if time.time() - target > MAX_LATE_S:
                self.db.finish_snapshot(snap_id, int(time.time()), None, True, "skipped", None, "skipped_too_late")
                return
            deep = self._deep_allowed(mint, label)
            if deep:
                # Dexscreener may not have indexed the PumpSwap pair yet: wait once (outside the deep
                # semaphore, so other work carries on) while still inside the tolerance
                retried = False
                all_pairs, failed = await self.dex.tokens_ex([mint])
                pairs = [p for p in all_pairs if (p.get("baseToken") or {}).get("address") == mint]
                if (not failed
                        and prescreen(pairs).get("reason") in ("no_pair", "pair_not_indexed")
                        and target + tol - time.time() > 200):
                    await asyncio.sleep(180)
                    retried = True
                async with self.deep_sem:
                    if time.time() - target > MAX_LATE_S:
                        self.db.finish_snapshot(snap_id, int(time.time()), None, True, "skipped", None,
                                                "skipped_too_late")
                        return
                    raw, slot = await take_snapshot(self.h, self.dex, self.db, mint, label, True)
                    raw["retried"] = retried
            else:
                raw, slot = await take_snapshot(self.h, self.dex, self.db, mint, label, False)
            late = raw["started_ts"] - target > tol
            kind = "deep" if raw.get("deep") else "light"
            self.db.finish_snapshot(snap_id, raw["started_ts"], slot, late, kind, raw)
            self.store_features(mint)
        except Exception as e:
            self.db.event("error_snapshot", f"{mint} {label}: {type(e).__name__}: {e}"[:500])
            log.warning("snapshot %s %s failed: %s", mint, label, type(e).__name__)
            self.db.finish_snapshot(snap_id, int(time.time()), None, time.time() - target > tol, "error", None,
                                    f"{type(e).__name__}: {e}"[:500])
        finally:
            self.running.discard(snap_id)

    def store_features(self, mint: str) -> None:
        rows = self.db.conn.execute("SELECT id, label, raw FROM snapshots WHERE mint=? AND status='done'",
                                    (mint,)).fetchall()
        feats, ids = {}, {}
        for r in rows:
            raw = unpack(r["raw"])
            if raw is None:
                continue
            feats[r["label"]] = F.compute(raw)
            ids[r["label"]] = r["id"]
        F.add_trend(feats)
        for label, f in feats.items():
            self.db.put_features(ids[label], mint, label, FEATURE_VERSION, f)

    async def snapshot_worker(self) -> None:
        while True:
            try:
                for row in self.db.due_snapshots(int(time.time())):
                    if row["id"] in self.running or not self.db.claim_snapshot(row["id"]):
                        continue
                    self.running.add(row["id"])
                    self.spawn(self.run_snapshot(row))
            except Exception as e:
                self.db.event("error_worker", type(e).__name__)
                log.exception("snapshot worker")
            await asyncio.sleep(5)

    # ---------- prices ----------
    async def price_tick(self) -> None:
        now = int(time.time())
        mints = self.db.tracked_mints(now, SAMPLE_DAYS)
        if not mints:
            return
        pairs, failed = await self.dex.tokens_ex(mints)
        by_mint = pairs_by_mint(pairs)
        matched = sum(1 for m in mints if m in by_mint)
        # Outage guard: an empty or near-empty answer for many tokens is a Dexscreener problem, not mass death
        if len(mints) >= 20 and matched < 0.05 * len(mints):
            self.db.event("dex_outage", f"{matched}/{len(mints)} matched")
            return
        self.db.conn.execute("BEGIN")
        try:
            for m in mints:
                if m in failed:  # request failed: no sample, rather than a false 'no pair'
                    continue
                p = sol_pumpswap_pair(by_mint.get(m, []))
                prev = self.db.last_sample(m)
                if p:
                    liq = (p.get("liquidity") or {}).get("usd")
                    self.db.add_price(m, now, float(p["priceNative"]) if p.get("priceNative") else None,
                                      float(p["priceUsd"]) if p.get("priceUsd") else None, liq, True)
                    prev_low = prev is not None and prev["pair_found"] and prev["liq_usd"] is not None \
                        and prev["liq_usd"] < DEAD_LIQ_USD
                    if liq is not None and liq < DEAD_LIQ_USD and prev_low:
                        self.db.mark_dead(m, prev["ts"])
                else:
                    self.db.add_price(m, now, None, None, None, False)
                    ref = self.db.last_pair_ts(m) or self.db.first_price_ts(m)
                    if ref is not None and now - ref >= DEAD_NO_DATA_HOURS * 3600:
                        self.db.mark_dead(m, now)
            self.db.conn.execute("COMMIT")
        except Exception:
            self.db.conn.execute("ROLLBACK")
            raise

    async def price_loop(self) -> None:
        while True:
            started = time.time()
            try:
                await self.price_tick()
            except Exception as e:
                self.db.event("error_prices", type(e).__name__)
                log.exception("price loop")
            await asyncio.sleep(max(5, self.s.price_interval_s - (time.time() - started)))

    # ---------- housekeeping ----------
    async def housekeeping(self) -> None:
        tz = ZoneInfo(self.s.timezone)
        while True:
            await asyncio.sleep(60)
            try:
                day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                self.db.count_calls(day, self.h.take_counts())
                healthy = self.ws_connected and time.time() - self.last_migration_ts < 3 * 3600
                if self.s.healthcheck_url and healthy:
                    try:
                        await self.http.get(self.s.healthcheck_url, timeout=10)
                    except httpx.HTTPError:
                        pass
                local = dt.datetime.now(tz)
                key = local.strftime("%Y-%m-%d")
                if local.hour >= self.s.report_hour_local and self.db.get_meta("last_report") != key:
                    self.db.set_meta("last_report", key)
                    await self.tg.send(daily_summary(self.db, int(time.time())))
                if time.time() - self.last_migration_ts > 2 * 3600 and not self.alerted_quiet:
                    self.alerted_quiet = True
                    await self.tg.send("Warning: no migrations detected for 2 hours. Pump.fun may have changed "
                                       "its migration flow, or the connection is broken. Run the check command.")
            except Exception:
                log.exception("housekeeping")

    async def command_loop(self) -> None:
        self.tg.offset = int(self.db.get_meta("tg_offset") or 0)
        while True:
            cmds = await self.tg.commands()
            self.db.set_meta("tg_offset", str(self.tg.offset))
            for cmd in cmds:
                if cmd == "/status":
                    c = self.db.conn
                    pending = c.execute("SELECT COUNT(*) n FROM snapshots WHERE status='pending'").fetchone()["n"]
                    total = c.execute("SELECT COUNT(*) n FROM migrations").fetchone()["n"]
                    await self.tg.send(f"Running. Graduates logged: {total}. Pending snapshots: {pending}. "
                                       f"Signature queue: {self.db.pending_count()}. "
                                       f"Websocket: {'connected' if self.ws_connected else 'DOWN'}. "
                                       f"Mode: phase 1 (log only, no trading).")
                elif cmd == "/report":
                    await self.tg.send(daily_summary(self.db, int(time.time())))
                elif cmd in ("/halt", "/stop"):
                    await self.tg.send("Phase 1 does not trade, so there is nothing to halt.")
                else:
                    await self.tg.send("Commands: /status /report")
            await asyncio.sleep(1)

    async def run(self) -> None:
        n = self.db.reset_running()
        if n:
            log.info("reset %d interrupted snapshots", n)
        self.db.event("start", prereg_hash())
        if self.db.get_meta("last_start_msg_hash") != prereg_hash() or \
                time.time() - float(self.db.get_meta("last_start_msg_ts") or 0) > 3600:
            await self.tg.send(f"Screener started (phase 1, log only). Pre-registration hash {prereg_hash()}. "
                               f"Feature version {FEATURE_VERSION}.")
            self.db.set_meta("last_start_msg_hash", prereg_hash())
            self.db.set_meta("last_start_msg_ts", str(time.time()))
        await self.safe_backfill("startup")
        await asyncio.gather(self.listener(), self.backfill_loop(), self.processor(), self.snapshot_worker(),
                             self.price_loop(), self.housekeeping(), self.command_loop())
