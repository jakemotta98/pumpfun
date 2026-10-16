"""SQLite storage. One file, safe across restarts."""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS migrations (
    mint TEXT PRIMARY KEY, signature TEXT UNIQUE, slot INTEGER, block_time INTEGER,
    quote_mint TEXT, symbol TEXT, detected_at INTEGER, source TEXT, dead_ts INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, label TEXT, target_ts INTEGER,
    taken_ts INTEGER, slot INTEGER, late INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
    kind TEXT, raw BLOB, error TEXT, UNIQUE(mint, label)
);
CREATE INDEX IF NOT EXISTS ix_snap_due ON snapshots(status, target_ts);
CREATE TABLE IF NOT EXISTS curve (mint TEXT PRIMARY KEY, creator TEXT, raw BLOB, taken_ts INTEGER);
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY, n_sigs INTEGER, oldest_ts INTEGER, funder TEXT, profiled_ts INTEGER
);
CREATE TABLE IF NOT EXISTS hubs (address TEXT PRIMARY KEY, n_sigs INTEGER, checked_ts INTEGER);
CREATE TABLE IF NOT EXISTS features (
    snapshot_id INTEGER PRIMARY KEY, mint TEXT, label TEXT, version TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    mint TEXT, ts INTEGER, price_native REAL, price_usd REAL, liq_usd REAL, pair_found INTEGER
);
CREATE INDEX IF NOT EXISTS ix_prices ON prices(mint, ts);
CREATE TABLE IF NOT EXISTS rpc_calls (day TEXT, method TEXT, n INTEGER, PRIMARY KEY(day, method));
CREATE TABLE IF NOT EXISTS events (ts INTEGER, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS pending_sigs (
    signature TEXT PRIMARY KEY, source TEXT, tries INTEGER DEFAULT 0, next_ts INTEGER
);
CREATE TABLE IF NOT EXISTS processed_sigs (signature TEXT PRIMARY KEY, outcome TEXT, ts INTEGER);
"""


def pack(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode())


def unpack(blob: bytes | None) -> Any:
    if blob is None:
        return None
    return json.loads(zlib.decompress(blob).decode())


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    # meta
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    # events
    def event(self, kind: str, detail: str = "") -> None:
        self.conn.execute("INSERT INTO events VALUES (?,?,?)", (int(time.time()), kind, detail[:2000]))

    # durable signature queue (survives restarts)
    def queue_sig(self, sig: str, source: str) -> bool:
        if self.conn.execute("SELECT 1 FROM processed_sigs WHERE signature=?", (sig,)).fetchone():
            return False
        cur = self.conn.execute("INSERT OR IGNORE INTO pending_sigs VALUES (?,?,0,?)", (sig, source, int(time.time())))
        return cur.rowcount == 1

    def next_sigs(self, now: int, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pending_sigs WHERE next_ts<=? ORDER BY next_ts LIMIT ?",
                                 (now, limit)).fetchall()

    def retry_sig(self, sig: str, delay_s: int) -> int:
        self.conn.execute("UPDATE pending_sigs SET tries=tries+1, next_ts=? WHERE signature=?",
                          (int(time.time()) + delay_s, sig))
        r = self.conn.execute("SELECT tries FROM pending_sigs WHERE signature=?", (sig,)).fetchone()
        return r["tries"] if r else 0

    def done_sig(self, sig: str, outcome: str) -> None:
        self.conn.execute("DELETE FROM pending_sigs WHERE signature=?", (sig,))
        self.conn.execute("INSERT OR REPLACE INTO processed_sigs VALUES (?,?,?)", (sig, outcome, int(time.time())))

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) n FROM pending_sigs").fetchone()["n"]

    # migrations
    def has_migration(self, mint: str) -> bool:
        return self.conn.execute("SELECT 1 FROM migrations WHERE mint=?", (mint,)).fetchone() is not None

    def has_signature(self, sig: str) -> bool:
        return self.conn.execute("SELECT 1 FROM migrations WHERE signature=?", (sig,)).fetchone() is not None

    def add_migration(self, mint: str, signature: str, slot: int, block_time: int,
                      quote_mint: str | None, source: str, snapshots: dict) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO migrations (mint, signature, slot, block_time, quote_mint, detected_at, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (mint, signature, slot, block_time, quote_mint, int(time.time()), source),
        )
        if cur.rowcount == 0:
            return False
        for label, (offset, _tol) in snapshots.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO snapshots (mint, label, target_ts, status) VALUES (?,?,?, 'pending')",
                (mint, label, block_time + offset),
            )
        return True

    def due_snapshots(self, now: int, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE status='pending' AND target_ts<=? ORDER BY target_ts LIMIT ?",
            (now, limit),
        ).fetchall()

    def claim_snapshot(self, snap_id: int) -> bool:
        cur = self.conn.execute("UPDATE snapshots SET status='running' WHERE id=? AND status='pending'", (snap_id,))
        return cur.rowcount == 1

    def finish_snapshot(self, snap_id: int, taken_ts: int, slot: int | None, late: bool,
                        kind: str, raw: Any, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE snapshots SET status=?, taken_ts=?, slot=?, late=?, kind=?, raw=?, error=? WHERE id=?",
            ("error" if error else "done", taken_ts, slot, int(late), kind, pack(raw), error, snap_id),
        )

    def reset_running(self) -> int:
        """After a crash, snapshots left 'running' go back to pending (they will be marked late)."""
        return self.conn.execute("UPDATE snapshots SET status='pending' WHERE status='running'").rowcount

    def snapshot_raw(self, mint: str, label: str) -> Any:
        r = self.conn.execute(
            "SELECT raw FROM snapshots WHERE mint=? AND label=? AND status='done'", (mint, label)
        ).fetchone()
        return unpack(r["raw"]) if r else None

    # curve
    def get_curve(self, mint: str) -> dict | None:
        r = self.conn.execute("SELECT raw FROM curve WHERE mint=?", (mint,)).fetchone()
        return unpack(r["raw"]) if r else None

    def put_curve(self, mint: str, creator: str | None, raw: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO curve VALUES (?,?,?,?)", (mint, creator, pack(raw), int(time.time())))

    # wallet cache
    def get_wallet(self, address: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
        return dict(r) if r else None

    def put_wallet(self, address: str, n_sigs: int, oldest_ts: int | None, funder: str | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO wallets VALUES (?,?,?,?,?)",
                          (address, n_sigs, oldest_ts, funder, int(time.time())))

    def get_hub(self, address: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM hubs WHERE address=?", (address,)).fetchone()
        return dict(r) if r else None

    def put_hub(self, address: str, n_sigs: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO hubs VALUES (?,?,?)", (address, n_sigs, int(time.time())))

    # features
    def put_features(self, snapshot_id: int, mint: str, label: str, version: str, data: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO features VALUES (?,?,?,?,?)",
                          (snapshot_id, mint, label, version, json.dumps(data)))

    # prices
    def tracked_mints(self, now: int, track_days: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT mint FROM migrations WHERE block_time>=? AND dead_ts IS NULL", (now - track_days * 86400,)
        ).fetchall()
        return [r["mint"] for r in rows]

    def add_price(self, mint: str, ts: int, price_native: float | None, price_usd: float | None,
                  liq_usd: float | None, pair_found: bool) -> None:
        self.conn.execute("INSERT INTO prices VALUES (?,?,?,?,?,?)",
                          (mint, ts, price_native, price_usd, liq_usd, int(pair_found)))

    def mark_dead(self, mint: str, ts: int) -> None:
        self.conn.execute("UPDATE migrations SET dead_ts=? WHERE mint=? AND dead_ts IS NULL", (ts, mint))

    def last_sample(self, mint: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()

    def last_pair_ts(self, mint: str) -> int | None:
        r = self.conn.execute("SELECT MAX(ts) t FROM prices WHERE mint=? AND pair_found=1", (mint,)).fetchone()
        return r["t"] if r else None

    def first_price_ts(self, mint: str) -> int | None:
        r = self.conn.execute("SELECT MIN(ts) t FROM prices WHERE mint=?", (mint,)).fetchone()
        return r["t"] if r else None

    def prices(self, mint: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM prices WHERE mint=? ORDER BY ts", (mint,)).fetchall()

    def set_symbol(self, mint: str, symbol: str | None, quote_mint: str | None) -> None:
        self.conn.execute(
            "UPDATE migrations SET symbol=COALESCE(symbol, ?), quote_mint=COALESCE(quote_mint, ?) WHERE mint=?",
            (symbol, quote_mint, mint),
        )

    # rpc accounting
    def count_calls(self, day: str, counts: dict[str, int]) -> None:
        for method, n in counts.items():
            self.conn.execute(
                "INSERT INTO rpc_calls VALUES (?,?,?) ON CONFLICT(day, method) DO UPDATE SET n=n+excluded.n",
                (day, method, n),
            )

    def features_data(self, mint: str, label: str) -> dict | None:
        r = self.conn.execute("SELECT data FROM features WHERE mint=? AND label=?", (mint, label)).fetchone()
        return json.loads(r["data"]) if r else None
