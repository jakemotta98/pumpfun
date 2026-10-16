"""Settings, loaded from environment variables (the .env file written by install.sh)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

FEATURE_VERSION = "1.1"

# On-chain constants. Checked against public docs on 16/10/2026; `python -m screener check`
# re-verifies them against live data on the server.
MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# Snapshot schedule: label -> (seconds after T0, tolerance seconds)
SNAPSHOTS = {
    "15m": (15 * 60, 5 * 60),
    "1h": (60 * 60, 5 * 60),
    "4h": (4 * 3600, 15 * 60),
    "24h": (24 * 3600, 15 * 60),
}
DEEP_LABELS = ("1h", "4h", "24h")  # 15m is Dexscreener-only

# Thresholds (PREREGISTRATION.md)
MIN_LIQ_USD = 15_000
CLUSTER_MAX_PCT = 15.0
DEV_MAX_PCT = 5.0
TOP10_MAX_PCT = 30.0
SNIPER_MAX_PCT = 10.0
HUB_SIG_THRESHOLD = 1000
ESTABLISHED_AGE_DAYS = 30
DEAD_LIQ_USD = 1_000
DEAD_NO_DATA_HOURS = 24
TRACK_DAYS = 7          # outcome window after entry
SAMPLE_DAYS = 9         # price sampling after T0 (covers a T+24h entry + 7 days)
PUMPSWAP_DEX_IDS = {"pumpswap"}  # Dexscreener dexId; `check` prints what it sees
CURVE_MAX_PAGES = 10
TOP_N_HOLDERS = 20
FIRST_SLOT_TX_CAP = 25


def _get(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


@dataclass
class Settings:
    helius_key: str
    telegram_token: str | None
    telegram_chat_id: str | None
    db_path: Path
    rpc_rps: float = 6.0          # Helius free tier allows 10; DAS shares this budget
    das_rps: float = 1.5          # free tier allows 2
    price_interval_s: int = 300
    backfill_interval_s: int = 600
    report_hour_local: int = 8
    timezone: str = "Australia/Perth"
    healthcheck_url: str | None = None
    deep_concurrency: int = 2
    extra: dict = field(default_factory=dict)

    @property
    def rpc_url(self) -> str:
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}"

    @property
    def ws_url(self) -> str:
        return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_key}"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    load_env_file(root / ".env")
    key = _get("HELIUS_API_KEY")
    if not key:
        raise SystemExit("HELIUS_API_KEY is missing. Run install.sh or edit .env")
    return Settings(
        helius_key=key,
        telegram_token=_get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=_get("TELEGRAM_CHAT_ID") or None,
        db_path=Path(_get("DB_PATH", str(root / "data" / "screener.db"))),
        rpc_rps=float(_get("RPC_RPS", "6")),
        das_rps=float(_get("DAS_RPS", "1.5")),
        report_hour_local=int(_get("REPORT_HOUR", "8")),
        timezone=_get("TIMEZONE", "Australia/Perth"),
        healthcheck_url=_get("HEALTHCHECK_URL") or None,
        deep_concurrency=int(_get("DEEP_CONCURRENCY", "2")),
    )
