import asyncio
import time

import pytest
from solders.keypair import Keypair

from screener import features as F
from screener.config import (INCINERATOR, MIGRATION_ACCOUNT, PUMP_PROGRAM, PUMPSWAP_PROGRAM, SNAPSHOTS, USDC_MINT,
                             WSOL_MINT)
from screener.db import DB
from screener.migration import parse_migration
from screener.onchain import bonding_curve_pda, find_funder, is_on_curve, token_deltas
from screener.outcomes import dead_from, measure
from screener.snapshot import prescreen, take_snapshot


def addr():
    return str(Keypair().pubkey())


MINT = "8tT2jWm3Yp7SgR8mT9rDZ2nHGh2Fq3pXqE4b4sS1pump"  # 44 chars base58, valid shape
MINT = str(Keypair().pubkey())


def bal(mint, owner, amount, idx=0):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "uiAmount": amount / 1e6, "decimals": 6}}


def migration_tx(mint, quote=WSOL_MINT, err=None):
    lp = addr()
    return {
        "slot": 123, "blockTime": 1_700_000_000,
        "meta": {"err": err, "logMessages": ["Program log: Instruction: Migrate"],
                 "preTokenBalances": [bal(mint, addr(), 206_900_000_000_000), bal(quote, addr(), 0)],
                 "postTokenBalances": [bal(mint, addr(), 206_900_000_000_000), bal(quote, addr(), 85),
                                       bal(lp, addr(), 1000)],
                 "innerInstructions": [{"instructions": [{"programId": PUMPSWAP_PROGRAM}]}]},
        "transaction": {"message": {"accountKeys": [{"pubkey": addr(), "signer": True}],
                                    "instructions": [{"programId": PUMP_PROGRAM}]}},
    }


def test_parse_migration_sol_and_usdc():
    m = parse_migration(migration_tx(MINT), "sig1")
    assert m and m.mint == MINT and m.quote_mint == WSOL_MINT and m.block_time == 1_700_000_000
    m2 = parse_migration(migration_tx(MINT, quote=USDC_MINT), "sig2")
    assert m2.quote_mint == USDC_MINT


def test_parse_migration_rejects_failed_or_other():
    assert parse_migration(migration_tx(MINT, err={"x": 1}), "s") is None
    tx = migration_tx(MINT)
    tx["meta"]["logMessages"] = ["Program log: Instruction: Buy"]
    assert parse_migration(tx, "s") is None
    tx = migration_tx(MINT)
    tx["meta"]["innerInstructions"] = []
    assert parse_migration(tx, "s") is None


def test_pda_off_curve_and_wallet_on_curve():
    assert not is_on_curve(bonding_curve_pda(MINT))
    assert is_on_curve(addr())


def test_token_deltas_and_funder():
    a, b = addr(), addr()
    tx = {"meta": {"preTokenBalances": [bal(MINT, a, 100)],
                   "postTokenBalances": [bal(MINT, a, 40), bal(MINT, b, 60)]}}
    assert token_deltas(tx, MINT) == {a: -60, b: 60}
    funder, wallet = addr(), addr()
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": addr()}], "instructions": [
        {"programId": "11111111111111111111111111111111",
         "parsed": {"type": "transfer", "info": {"source": funder, "destination": wallet, "lamports": 1}}}]}},
          "meta": {}}
    assert find_funder(tx, wallet) == funder


def test_prescreen():
    base = {"baseToken": {"address": MINT}, "dexId": "pumpswap", "quoteToken": {"address": WSOL_MINT},
            "liquidity": {"usd": 20_000}}
    assert prescreen([base])["pass"]
    assert prescreen([{**base, "liquidity": {"usd": 5000}}])["reason"] == "low_liquidity"
    assert prescreen([{**base, "quoteToken": {"address": USDC_MINT}}])["reason"] == "not_sol_quoted"
    assert prescreen([])["reason"] == "no_pair"


def _deep(holders, profiles, hubs=None, creator=None, fsb=None, balances=None, supply=1_000_000):
    return {"supply": {"amount": str(supply)},
            "holders": holders, "profiles": profiles, "hub_sigs": hubs or {},
            "curve": {"creator": creator, "first_slot_buys": fsb or {}, "complete": True},
            "balances": balances or {}, "holder_count": {"count": 500, "capped": False},
            "mint_account": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                             "data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None}}}}}


def test_holder_metrics_clusters_hubs_dev_snipers():
    pool = bonding_curve_pda(MINT)  # off-curve stand-in for a pool owner
    w = [addr() for _ in range(6)]
    f_shared, hub, creator = addr(), addr(), addr()
    holders = [{"owner": pool, "amount": 500_000, "owner_on_curve": False},
               {"owner": w[0], "amount": 90_000, "owner_on_curve": True},
               {"owner": w[1], "amount": 80_000, "owner_on_curve": True},
               {"owner": w[2], "amount": 30_000, "owner_on_curve": True},
               {"owner": w[3], "amount": 20_000, "owner_on_curve": True},
               {"owner": INCINERATOR, "amount": 100_000, "owner_on_curve": True}]
    now = int(time.time())
    profiles = {w[0]: {"funder": f_shared, "n_sigs": 5, "oldest_ts": now - 3600, "profiled_ts": now},
                w[1]: {"funder": f_shared, "n_sigs": 5, "oldest_ts": now - 3600, "profiled_ts": now},
                w[2]: {"funder": hub, "n_sigs": 1000, "oldest_ts": now - 100, "profiled_ts": now},
                w[3]: {"funder": hub, "n_sigs": 10, "oldest_ts": now - 40 * 86400, "profiled_ts": now},
                creator: {"funder": None, "n_sigs": 3, "oldest_ts": now, "profiled_ts": now}}
    deep = _deep(holders, profiles, hubs={f_shared: 3, hub: 1000}, creator=creator,
                 fsb={w[0]: 50_000, w[4]: 10_000}, balances={creator: 10_000, w[4]: 0, w[0]: 90_000})
    hm = F.holder_metrics(deep)
    assert hm["top10_pct"] == pytest.approx(22.0)            # pool and incinerator excluded
    assert hm["max_cluster_pct"] == pytest.approx(17.0)      # w0+w1 share a non-hub funder
    assert hm["n_clusters"] == 1                             # hub-funded w2/w3 not linked
    assert hm["dev_pct"] == pytest.approx(1.0)
    assert hm["sniper_pct"] == pytest.approx(9.0)
    assert hm["established_share"] == pytest.approx(0.5)     # w2 by count, w3 by age
    flow = {"quote": WSOL_MINT, "liq_usd": 20_000}
    assert F.kills(flow, hm, F.mint_risks(deep)) == ["K4_cluster"]


def test_kills_authority_and_token2022():
    deep = _deep([], {})
    deep["mint_account"] = {"owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                            "data": {"parsed": {"info": {"mintAuthority": addr(), "freezeAuthority": None,
                                                         "extensions": [{"extension": "transferHook"}]}}}}
    k = F.kills({"quote": WSOL_MINT, "liq_usd": 1}, F.holder_metrics(deep), F.mint_risks(deep))
    assert {"K2_authority", "K3_token2022", "K8_liquidity"} <= set(k)


def _samples(prices, start=0, step=300, liq=50_000):
    return [{"ts": start + i * step, "price_native": p, "price_usd": p, "liq_usd": liq if p else None,
             "pair_found": p is not None} for i, p in enumerate(prices)]


def test_outcomes_basic_and_dead():
    t0 = 0
    s = _samples([1.0] * 12 + [2.0] * 300, start=0)  # doubles after 1h
    oc = measure(s, ref_ts=0, t0=t0)
    assert oc["entry_price_native"] == 1.0
    assert oc["mult_1h"] == pytest.approx(2.0)
    assert oc["max_mult"] == pytest.approx(2.0)
    assert oc["mult_7d"] is None  # not observable yet

    low = lambda ts: {"ts": ts, "price_native": 0.5, "price_usd": 0.5, "liq_usd": 500, "pair_found": True}
    one_dip = _samples([1.0, 1.2, 0.8]) + [low(900)] + _samples([0.5] * 10, start=1200)
    assert dead_from(one_dip) is None  # a single low-liquidity sample is not death
    s = _samples([1.0, 1.2, 0.8]) + [low(900), low(1200)] + _samples([0.5] * 400, start=1500)
    assert dead_from(s) == 900
    oc = measure(s, ref_ts=0, t0=t0)
    assert oc["dead"] and oc["mult_24h"] == 0.0 and oc["max_drawdown"] == -1.0


def test_dead_no_data_24h():
    s = _samples([1.0]) + [{"ts": 300 + i * 300, "price_native": None, "price_usd": None, "liq_usd": None,
                            "pair_found": False} for i in range(300)]
    assert dead_from(s) == 86400


def test_db_schedule(tmp_path):
    db = DB(tmp_path / "t.db")
    assert db.add_migration(MINT, "sig", 1, 1000, WSOL_MINT, "ws", SNAPSHOTS)
    assert not db.add_migration(MINT, "sig", 1, 1000, WSOL_MINT, "ws", SNAPSHOTS)
    due = db.due_snapshots(1000 + 3600)
    assert {r["label"] for r in due} == {"15m", "1h"}
    assert db.claim_snapshot(due[0]["id"]) and not db.claim_snapshot(due[0]["id"])
    assert db.reset_running() == 1


class FakeHelius:
    """Answers the calls deep_state makes, with a small consistent world."""

    def __init__(self, mint, holders, creator):
        self.mint, self.holders, self.creator = mint, holders, creator
        self.token_accounts = {o: addr() for o, _ in holders}

    async def get_slot(self):
        return 999

    async def get_account_info(self, a):
        return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None}}}}

    async def get_token_supply(self, m):
        return {"amount": "1000000", "decimals": 6}

    async def get_token_largest_accounts(self, m):
        return [{"address": self.token_accounts[o], "amount": str(a)} for o, a in self.holders]

    async def get_multiple_accounts(self, addrs):
        inv = {v: k for k, v in self.token_accounts.items()}
        return [{"data": {"parsed": {"info": {"owner": inv[a]}}}} for a in addrs]

    async def get_signatures(self, address, limit=1000, before=None, until=None):
        if address == bonding_curve_pda(self.mint):
            return [{"signature": "later", "slot": 11, "err": None}, {"signature": "create", "slot": 10, "err": None}]
        return [{"signature": f"first-{address}", "slot": 1, "blockTime": 1, "err": None}]

    async def get_transaction(self, sig):
        if sig == "create":
            return {"transaction": {"message": {"accountKeys": [{"pubkey": self.creator}], "instructions": []}},
                    "meta": {"logMessages": ["Program log: Instruction: Create"],
                             "preTokenBalances": [], "postTokenBalances": [bal(self.mint, self.creator, 30_000)]}}
        wallet = sig.split("-", 1)[1]
        return {"transaction": {"message": {"accountKeys": [{"pubkey": wallet}], "instructions": []}}, "meta": {}}

    async def get_token_accounts_by_owner(self, owner, mint):
        return [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "0"}}}}}}]

    async def das_token_accounts(self, mint, limit=1000, cursor=None):
        return {"token_accounts": [{"amount": 1}] * 42, "cursor": None}


class FakeDex:
    def __init__(self, mint):
        self.mint = mint

    async def tokens_ex(self, mints):
        return await self.tokens(mints), set()

    async def tokens(self, mints):
        return [{"baseToken": {"address": self.mint, "symbol": "TEST"}, "dexId": "pumpswap",
                 "quoteToken": {"address": WSOL_MINT}, "liquidity": {"usd": 30_000}, "priceNative": "0.000001",
                 "priceUsd": "0.0002", "txns": {"h1": {"buys": 120, "sells": 80}}, "volume": {"h1": 40_000},
                 "info": {"socials": [{"type": "twitter", "url": "x"}]}}]


def test_end_to_end_snapshot(tmp_path):
    db = DB(tmp_path / "e.db")
    creator = addr()
    holders = [(bonding_curve_pda(MINT), 600_000), (addr(), 50_000), (addr(), 40_000)]
    raw, slot = asyncio.run(take_snapshot(FakeHelius(MINT, holders, creator), FakeDex(MINT), db, MINT, "4h", True))
    assert slot == 999 and raw["prescreen"]["pass"] and raw["deep"]
    assert raw["deep"]["curve"]["creator"] == creator
    assert raw["deep"]["curve"]["first_slot_buys"] == {creator: 30_000}
    f = F.compute(raw)
    assert f["holders"]["top10_pct"] == pytest.approx(9.0)
    assert f["holders"]["holder_count"] == 42
    assert f["flow"]["buys_per_k_vol"] == pytest.approx(3.0)
    assert f["flow"]["has_twitter"]
    assert f["kills"] == []
