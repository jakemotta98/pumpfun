"""Command line: python -m screener [run|check|report|export]"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets
from solders.pubkey import Pubkey

from .app import App, prereg_hash
from .clients import Dexscreener, Helius, RedactFilter, main_pair, sol_pumpswap_pair
from .config import MIGRATION_ACCOUNT, PUMP_PROGRAM, load_settings
from .db import DB
from .migration import parse_migration
from .onchain import analyse_curve, bonding_curve_pda, is_on_curve
from .report import daily_summary, export_csv
from .telegram import Telegram


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")


async def check() -> int:
    """Verifies every assumption the bot depends on, against live data. Run this after install."""
    s = load_settings()
    failures = 0
    async with httpx.AsyncClient(timeout=30) as http:
        h = Helius(s.rpc_url, 5, 1, http)
        dex = Dexscreener(http=http)
        print("1. Helius connection")
        try:
            ok(f"current slot {await h.get_slot()}")
        except Exception as e:
            bad(f"Helius not reachable or key wrong: {e}")
            return 1

        print("2. Recent migrations (the bot's core assumption)")
        sigs = await h.get_signatures(MIGRATION_ACCOUNT, limit=25)
        if not sigs:
            bad("no transactions found for the migration account - Pump.fun may have changed it")
            return 1
        parsed = []
        tx_errors = 0
        diagnosed = False
        for sg in sigs:
            if sg.get("err") is not None:
                continue
            try:
                tx = await h.get_transaction(sg["signature"])
            except Exception as e:
                tx_errors += 1
                if tx_errors == 1:
                    bad(f"getTransaction failed: {e}")
                continue
            m = parse_migration(tx, sg["signature"])
            if m:
                parsed.append(m)
            elif tx and not diagnosed and "Migrate" in " ".join((tx.get("meta") or {}).get("logMessages") or []):
                diagnosed = True  # a migration we could not parse: print its shape so it can be fixed
                meta = tx.get("meta") or {}
                msg = (tx.get("transaction") or {}).get("message") or {}
                from .migration import _program_ids
                print("  INFO  unparsed migration", sg["signature"])
                print("  INFO   version", tx.get("version"), "| blockTime", tx.get("blockTime"),
                      "| err", meta.get("err"))
                print("  INFO   message keys", sorted(msg.keys()), "| meta keys", sorted(meta.keys()))
                print("  INFO   programs", sorted(_program_ids(tx)))
                print("  INFO   preTokenBalances", [(b.get("mint"), b.get("owner")) for b in
                                                    meta.get("preTokenBalances") or []][:6])
        if tx_errors:
            failures += 1
        ok(f"{len(sigs)} recent transactions read, {len(parsed)} parsed as migrations "
           f"({sum(m.method == 'curve_owner' for m in parsed)} identified via the curve account, "
           f"{sum(m.method != 'curve_owner' for m in parsed)} by fallback heuristic)")
        if not parsed:
            bad("none parsed as migrations - the parser needs updating before running")
            failures += 1
        else:
            newest = max(parsed, key=lambda m: m.block_time)
            mins = (time.time() - newest.block_time) / 60
            ok(f"newest migration {mins:.0f} min ago: {newest.mint} (quote {newest.quote_mint})")
            span_h = (max(m.block_time for m in parsed) - min(m.block_time for m in parsed)) / 3600 or 1
            ok(f"rough rate: {len(parsed) / span_h:.1f} migrations/hour from this sample")

            print("3. Bonding curve address derivation")
            curve = bonding_curve_pda(newest.mint)
            acct = await h.get_account_info(curve)
            if acct and acct.get("owner") == PUMP_PROGRAM:
                ok(f"curve account {curve} found, owned by the Pump.fun program")
            else:
                bad(f"curve account {curve} not found or wrong owner ({(acct or {}).get('owner')})")
                failures += 1

            # creator: curve-account bytes (offset 49, unverified layout) vs Create-tx fee payer, report only
            try:
                cv = await analyse_curve(h, newest.mint)
                res = await h.call("getAccountInfo", [curve, {"encoding": "base64"}])
                data = base64.b64decode(((res or {}).get("value") or {}).get("data", [""])[0])
                from_bytes = str(Pubkey.from_bytes(data[49:81])) if len(data) >= 81 else None
                line = (f"creation lookup via {cv.get('method')}: complete={cv['complete']}, creator {cv['creator']}, "
                        f"creation-slot buyers {len(cv['first_slot_buys'])} from {cv.get('first_slot_txs_read')} txs")
                if cv.get("primary_failure"):
                    line += f" (primary method failed: {cv['primary_failure']})"
                (ok if cv["complete"] and cv["creator"] else bad)(line)
                if not (cv["complete"] and cv["creator"]):
                    failures += 1
                print(f"  INFO  creator from curve bytes: {from_bytes} "
                      f"({'matches' if from_bytes == cv['creator'] else 'does not match'}; layout unverified, info only)")
            except Exception as e:
                bad(f"curve analysis failed: {type(e).__name__}: {e}")
                failures += 1

            print("4. Holders")
            try:
                largest = await h.get_token_largest_accounts(newest.mint)
                accts = await h.get_multiple_accounts([a["address"] for a in largest[:20]])
                owners = [((a or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("owner") for a in accts]
                n_off = sum(1 for o in owners if o and not is_on_curve(o))
                if all(owners):
                    ok(f"top {len(owners)} holders resolved to owners; {n_off} program-owned (pool/curve)")
                else:
                    bad("some holder owners could not be resolved")
                    failures += 1
                das = await h.das_token_accounts(newest.mint, limit=1000)
                ok(f"DAS holder list: {len(das.get('token_accounts') or [])} accounts on page 1, "
                   f"cursor {'present' if das.get('cursor') else 'absent'}")
            except Exception as e:
                bad(f"holder reads failed: {type(e).__name__}: {e}")
                failures += 1

            print("5. Dexscreener")
            pairs = await dex.tokens([m.mint for m in parsed[:10]])
            if pairs:
                ids = sorted({p.get('dexId') or '' for p in pairs})
                ok(f"{len(pairs)} pairs returned; dex ids seen: {ids}")
                mine = [q for q in pairs if (q.get('baseToken') or {}).get('address') == newest.mint]
                p = sol_pumpswap_pair(mine) or main_pair(mine)
                if p:
                    ok(f"newest token pair: dex {p.get('dexId')}, quote {(p.get('quoteToken') or {}).get('symbol')}, "
                       f"liquidity ${(p.get('liquidity') or {}).get('usd')}, txns keys "
                       f"{sorted((p.get('txns') or {}).keys())}")
                if not any(sol_pumpswap_pair([q]) for q in pairs):
                    bad("no SOL-quoted 'pumpswap' pair seen - the dex id rule in config.py needs adjusting")
                    failures += 1
            else:
                bad("Dexscreener returned no pairs for recent graduates (may just be indexing lag; rerun in 10 min)")

    print("6. Live websocket (waits up to 3 minutes for one migration notification)")
    try:
        async with websockets.connect(s.ws_url, ping_interval=20) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                      "params": [{"mentions": [MIGRATION_ACCOUNT]}, {"commitment": "confirmed"}]}))
            deadline = time.time() + 180
            got = False
            while time.time() < deadline and not got:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time())))
                val = (((msg.get("params") or {}).get("result") or {}).get("value")) or {}
                got = bool(val.get("signature"))
            ok("notification received") if got else bad("no notification in 3 minutes (rerun later)")
    except asyncio.TimeoutError:
        print("  WARN  no notification in 3 minutes; not fatal (graduations can be slow), backfill covers it")
    except Exception as e:
        bad(f"websocket failed: {type(e).__name__}")
        failures += 1

    print("7. System")
    try:
        sync = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        ok("clock synchronised") if sync == "yes" else print(f"  WARN  clock sync status: {sync or 'unknown'}")
    except Exception:
        print("  WARN  could not read clock sync status")
    free_gb = shutil.disk_usage(s.db_path.parent if s.db_path.parent.exists() else ".").free / 1e9
    ok(f"{free_gb:.0f} GB disk free") if free_gb >= 5 else bad(f"only {free_gb:.1f} GB disk free")

    print("8. Telegram")
    tg = Telegram(s.telegram_token, s.telegram_chat_id)
    if tg.enabled:
        await tg.send("Test message from your screener. If you can read this, Telegram is set up.")
        ok("test message sent - check your phone")
    else:
        bad("Telegram token or chat id missing in .env")
        failures += 1
    print(f"9. Pre-registration hash: {prereg_hash()}")
    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed. Send the output to Claude.")
    return 1 if failures else 0


async def diagnose(n: int = 4) -> None:
    """Show what the logged 'unparsed_migration' transactions actually are."""
    from .migration import _program_ids
    from .onchain import bonding_curve_pda
    s = load_settings()
    db = DB(s.db_path)
    rows = db.conn.execute("SELECT detail FROM events WHERE kind='unparsed_migration' ORDER BY ts DESC LIMIT ?",
                           (n,)).fetchall()
    total = db.conn.execute("SELECT COUNT(*) n FROM events WHERE kind='unparsed_migration'").fetchone()["n"]
    print(f"{total} unparsed migration transactions logged; showing {len(rows)}\n")
    async with httpx.AsyncClient(timeout=30) as http:
        h = Helius(s.rpc_url, 5, 1, http)
        for r in rows:
            parts = (r["detail"] or "").split()
            sig = parts[0]
            if len(parts) > 1 and parts[1] != "-":
                print(f"token {parts[1]} already logged as a migration: {db.has_migration(parts[1])}")
            tx = await h.get_transaction(sig)
            meta = (tx or {}).get("meta") or {}
            print("signature:", sig)
            print("  version:", (tx or {}).get("version"), "| blockTime:", (tx or {}).get("blockTime"))
            print("  instruction logs:", [l.strip()[:80] for l in meta.get("logMessages") or [] if "Instruction:" in l][:8])
            progs = _program_ids(tx or {})
            print("  pump program:", PUMP_PROGRAM in progs, "| pumpswap program:",
                  "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA" in progs, "| n programs:", len(progs))
            pre = meta.get("preTokenBalances") or []
            print("  preTokenBalances:", len(pre), "| postTokenBalances:", len(meta.get("postTokenBalances") or []))
            for b in pre[:6]:
                m, o = b.get("mint"), b.get("owner")
                own = False
                try:
                    own = bool(m and o and bonding_curve_pda(m) == o)
                except Exception:
                    pass
                print(f"    mint {m} owner {o} owner_is_curve={own}")
            print()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        asyncio.run(App(load_settings()).run())
    elif cmd == "check":
        sys.exit(asyncio.run(check()))
    elif cmd == "diagnose":
        asyncio.run(diagnose())
    elif cmd == "report":
        print(daily_summary(DB(load_settings().db_path), int(time.time())))
    elif cmd == "export":
        out = Path(sys.argv[2] if len(sys.argv) > 2 else "export.csv")
        n = export_csv(DB(load_settings().db_path), out)
        print(f"wrote {n} rows to {out}")
    else:
        print("usage: python -m screener [run|check|diagnose|report|export [file.csv]]")
        sys.exit(2)


if __name__ == "__main__":
    main()
