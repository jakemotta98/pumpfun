"""Outcome measurement from the 5-minute price samples (SOL terms). Definitions are in PREREGISTRATION.md."""
from __future__ import annotations

from typing import Iterable

from .config import DEAD_LIQ_USD, DEAD_NO_DATA_HOURS, TRACK_DAYS

HORIZONS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


def dead_from(samples: list[dict]) -> int | None:
    """Timestamp of the first sample at which the token counts as dead, else None."""
    last_pair_ts = None
    first_ts = samples[0]["ts"] if samples else None
    low_streak_start = None
    for s in samples:
        if s["pair_found"]:
            if s["liq_usd"] is not None and s["liq_usd"] < DEAD_LIQ_USD:
                if low_streak_start is not None:
                    return low_streak_start  # two consecutive low samples; dead from the first
                low_streak_start = s["ts"]
            else:
                low_streak_start = None
            last_pair_ts = s["ts"]
        else:
            low_streak_start = None
            ref = last_pair_ts if last_pair_ts is not None else first_ts
            if ref is not None and s["ts"] - ref >= DEAD_NO_DATA_HOURS * 3600:
                return s["ts"]
    return None


def measure(samples: Iterable[dict], ref_ts: int, t0: int) -> dict | None:
    """Entry = first priced sample at or after ref_ts. Returns multiples, max multiple, max drawdown, dead flag."""
    samples = sorted((dict(s) for s in samples), key=lambda s: s["ts"])
    if not samples:
        return None
    dts = dead_from(samples)
    entry = next((s for s in samples if s["ts"] >= ref_ts and s["pair_found"] and s["price_native"]), None)
    if entry is None or (dts is not None and dts <= entry["ts"]):
        return {"entry_ts": None, "dead_before_entry": dts is not None}
    p0 = entry["price_native"]
    end = entry["ts"] + TRACK_DAYS * 86400  # outcome window: 7 days after entry

    def price_at(ts: int) -> float | None:
        if dts is not None and dts <= ts:
            return 0.0
        prior = [s for s in samples if s["ts"] <= ts and s["pair_found"] and s["price_native"]]
        return prior[-1]["price_native"] if prior else None

    out: dict = {"entry_ts": entry["ts"], "entry_price_native": p0, "entry_lag_s": entry["ts"] - ref_ts}
    last_ts = samples[-1]["ts"]
    for name, h in HORIZONS.items():
        t = entry["ts"] + h
        if t > last_ts and (dts is None or dts > t):
            out[f"mult_{name}"] = None  # not yet observable
        else:
            p = price_at(t)
            out[f"mult_{name}"] = (p / p0) if p is not None else None
    window = [s for s in samples if entry["ts"] <= s["ts"] <= end and s["pair_found"] and s["price_native"]
              and (dts is None or s["ts"] < dts)]
    prices = [s["price_native"] for s in window] or [p0]
    out["max_mult"] = max(prices) / p0
    out["max_drawdown"] = -1.0 if dts is not None and dts <= end else (min(prices) / p0 - 1)
    out["dead"] = dts is not None and dts <= end
    out["dead_ts"] = dts
    out["complete"] = last_ts >= end or out["dead"]
    return out
