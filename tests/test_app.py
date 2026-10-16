import asyncio
import time

from screener.app import App
from screener.config import SNAPSHOTS, WSOL_MINT, Settings
from screener.onchain import bonding_curve_pda
from screener.report import daily_summary, export_csv

from test_core import MINT, FakeDex, FakeHelius, addr


def test_app_pipeline(tmp_path):
    s = Settings(helius_key="x", telegram_token=None, telegram_chat_id=None, db_path=tmp_path / "a.db")
    app = App(s)
    app.h = FakeHelius(MINT, [(bonding_curve_pda(MINT), 600_000), (addr(), 50_000)], addr())
    app.dex = FakeDex(MINT)
    t0 = int(time.time()) - 5 * 3600
    assert app.db.add_migration(MINT, "sig", 1, t0, WSOL_MINT, "ws", SNAPSHOTS)

    async def go():
        for row in app.db.due_snapshots(int(time.time())):
            assert app.db.claim_snapshot(row["id"])
            await app.run_snapshot(row)
    asyncio.run(go())
    rows = {r["label"]: dict(r) for r in app.db.conn.execute("SELECT * FROM snapshots")}
    assert rows["15m"]["kind"] == "light" and rows["1h"]["kind"] == "deep" and rows["4h"]["kind"] == "deep"
    assert rows["15m"]["late"] == 1 and rows["24h"]["status"] == "pending"
    feats = {r["label"]: r for r in app.db.conn.execute("SELECT * FROM features")}
    assert "trend" in __import__("json").loads(feats["4h"]["data"])
    for i in range(30):
        app.db.add_price(MINT, t0 + 4 * 3600 + i * 300, 1e-6 * (1 + i / 10), 0.0002, 30_000, True)
    print(daily_summary(app.db, int(time.time())))
    assert export_csv(app.db, tmp_path / "x.csv") == 3
    assert "mult_1h" in (tmp_path / "x.csv").read_text().splitlines()[0]


def test_parse_by_curve_owner():
    from screener.migration import parse_migration
    from test_core import bal, migration_tx
    tx = migration_tx(MINT)
    tx["meta"]["preTokenBalances"][0] = bal(MINT, bonding_curve_pda(MINT), 206_900_000_000_000)
    assert parse_migration(tx, "s").method == "curve_owner"


def test_durable_queue_and_outage_guard(tmp_path):
    from test_core import migration_tx
    s = Settings(helius_key="x", telegram_token=None, telegram_chat_id=None, db_path=tmp_path / "q.db")
    app = App(s)

    class H:
        calls = 0
        async def get_transaction(self, sig):
            H.calls += 1
            return None if sig == "missing" else migration_tx(MINT)
    app.h = H()
    app.db.queue_sig("good", "ws")
    app.db.queue_sig("missing", "ws")
    asyncio.run(app.process_sig("good"))
    asyncio.run(app.process_sig("missing"))
    assert app.db.has_migration(MINT)
    assert app.db.pending_count() == 1          # 'missing' stays queued for retry
    assert not app.db.queue_sig("good", "backfill")  # already processed, not re-queued

    class D:
        async def tokens_ex(self, mints):
            return [], set()
    app.dex = D()
    for i in range(25):
        app.db.add_migration(f"m{i}", f"sig{i}", 1, int(time.time()), WSOL_MINT, "ws", {})
    asyncio.run(app.price_tick())
    n = app.db.conn.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"]
    assert n == 0  # outage: nothing recorded, nothing marked dead


def test_24h_deep_only_for_4h_survivors(tmp_path):
    s = Settings(helius_key="x", telegram_token=None, telegram_chat_id=None, db_path=tmp_path / "g.db")
    app = App(s)
    assert not app._deep_allowed(MINT, "24h")
    app.db.put_features(1, MINT, "4h", "1.1", {"kills": []})
    assert app._deep_allowed(MINT, "24h") and app._deep_allowed(MINT, "1h") and not app._deep_allowed(MINT, "15m")


def test_failed_dex_chunk_is_not_no_pair(tmp_path):
    import pytest
    from screener.snapshot import take_snapshot
    s = Settings(helius_key="x", telegram_token=None, telegram_chat_id=None, db_path=tmp_path / "f.db")
    app = App(s)
    mints = [f"m{i}" for i in range(40)]
    for i, m in enumerate(mints):
        app.db.add_migration(m, f"sig{i}", 1, int(time.time()), WSOL_MINT, "ws", {})

    class D:
        async def tokens_ex(self, ms):
            pairs = [{"baseToken": {"address": m}, "dexId": "pumpswap", "quoteToken": {"address": WSOL_MINT},
                      "liquidity": {"usd": 30_000}, "priceNative": "1"} for m in ms[:30]]
            return pairs, set(ms[30:])  # second chunk failed
    app.dex = D()
    asyncio.run(app.price_tick())
    n = app.db.conn.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"]
    assert n == 30  # failed mints get no sample at all

    class Down:
        async def tokens_ex(self, ms):
            return [], set(ms)

    class H:
        async def get_slot(self):
            return 1
    with pytest.raises(RuntimeError):
        asyncio.run(take_snapshot(H(), Down(), app.db, "m0", "15m", False))
