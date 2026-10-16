# Pre-registration - phase 1 (log only)

Version: 1.1, written 16/10/2026, before any data was collected (1.1 folds in the pre-launch code review).
The SHA-256 of this file is printed in the first Telegram message the bot sends. Any later edit changes the hash, which is the point.

## Question

Does post-migration selection of Pump.fun graduates, using on-chain kill filters and a small equal-weighted score, produce better net returns (in SOL terms) than a random size-matched sample of graduates run through the same exits and costs?

Prior: probably not. Published work finds that backtested memecoin models often collapse on later data. Phase 1 exists to build a clean dataset; it does not test anything.

## Universe

- Every token that migrates from the Pump.fun bonding curve to PumpSwap, detected from transactions that mention the migration account `39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg`, log `Instruction: Migrate`, invoke both the Pump.fun program and the PumpSwap program, and have no transaction error and a block time.
- The graduating mint is the one held by its own bonding-curve account before migration (a fallback heuristic is logged as a warning whenever used).
- Every graduate is logged, including ones that fail filters or have no Dexscreener pair.
- Analysis set: SOL-quoted pools only. USDC-quoted pools are logged and excluded.

## Clock

- T0 = block time of the migration transaction (not the time the bot saw it).
- Snapshots at T+15m, T+1h, T+4h, T+24h. A snapshot is valid if taken within 5 minutes of target (15m, 1h) or 15 minutes (4h, 24h). Late snapshots are logged and flagged, never relabelled.
- T+15m is Dexscreener-only. Deep on-chain checks run at T+1h and T+4h for tokens passing the pre-screen, and at T+24h only for tokens that passed every kill filter at T+4h (credit budget).
- If the PumpSwap pair is not yet on Dexscreener at a deep snapshot, it is retaken once after 3 minutes when that still falls inside the tolerance.
- Single pre-chosen entry point for any test: T+4h. Other snapshots are logged only.
- One entry per token at most.

## Raw data rule

- Every snapshot stores the raw API responses with the slot at capture time.
- Features are derived only from stored snapshots. No feature may use a live call made after the snapshot.
- Each feature row records the feature-code version.

## Pre-screen (cheap, every snapshot)

A deep on-chain check runs only if, at that snapshot: a PumpSwap pair (Dexscreener dexId `pumpswap`) quoted in SOL exists, and its liquidity is at least US$15,000. All prices, liquidity and flow figures come from the deepest SOL-quoted PumpSwap pair, never from the bonding-curve pair.

## Kill filters (evaluated at each deep snapshot)

| Code | Rule |
|---|---|
| K0 | Bonding-curve history could not be read in full (creator or creation-slot buyers unknown). Excluded, never passed by default |
| K1 | Pool not SOL-quoted (in practice enforced by the pre-screen, so it should always read zero) |
| K2 | Mint authority or freeze authority not revoked |
| K3 | Token-2022 mint with transfer fee, transfer hook, permanent delegate or default-frozen state |
| K4 | Any linked-wallet cluster among top-20 holders above 15% of supply |
| K5 | Creator plus creator-funded wallets above 5% of supply |
| K6 | Top-10 holders above 30% of supply, excluding program-owned accounts (pools, curve) and the incinerator |
| K7 | Creation-slot buyers still holding above 10% of supply |
| K8 | Pool liquidity below US$15,000 |

Definitions:
- Creator = fee payer of the transaction in the creation slot whose logs show `Instruction: Create`; if none is found among the transactions read, the fee payer of the earliest transaction on the bonding-curve account.
- Creation-slot buyers = wallets that received the token in transactions in the same slot as that earliest transaction (first 25 transactions read). This includes the creator's own buy in the create transaction, so creator holdings can count towards both K5 and K7. Current balances are checked for the 10 largest; buys under 0.5% of supply are not rechecked unless the wallet is a top-20 holder.
- Creator-funded wallets (K5) = top-20 holders whose first incoming SOL came from the creator.
- Linked cluster = two or more top-20 holders whose first incoming SOL came from the same address. Funders with 1,000 or more transactions in their recent history are treated as exchanges or hubs and do not link wallets.

Not active in phase 1 (logged, not filtered):
- Deployer launch history. The creator address is logged; a history filter needs a verified data source first.
- Dev moving tokens to fresh wallets (partly covered by K5).

## Features (logged at every deep snapshot; score uses the T+4h values)

| Code | Feature | Direction |
|---|---|---|
| F1 | Holder quality: share of top-20 non-program holders that are established wallets (1,000+ transactions, or first activity 30+ days before the snapshot time) | Higher better |
| F2 | Flow proxy: buy transactions per US$1,000 of 1h volume (Dexscreener counts, not unique wallets) | Higher better |
| F3 | Distribution trend: change in top-10 share from T+1h to T+4h | Lower (falling) better |

Score = mean of the three percentile ranks, equal weights. No other weights will be fitted.

Logged only, not in the score: funder diversity, holder count and growth, buy/sell count ratio, socials present, X or Telegram link present, copycat symbol flag, drawdown from post-migration peak, liquidity retained vs T+15m, LLM narrative rating (if enabled later).

## Outcomes (every graduate, from every snapshot price)

- Price in SOL (Dexscreener `priceNative` on the SOL-quoted PumpSwap pair). Sampled every 5 minutes for 9 days after T0, so 7-day outcomes are observable from every snapshot including T+24h.
- Entry = first price sample at or after the snapshot's decision time (when its data capture finished).
- Multiple at +1h, +24h, +7d after entry; maximum multiple within 7 days after entry; maximum drawdown = lowest price within 7 days after entry relative to entry price (not peak-to-trough).
- Dead: two consecutive samples with pool liquidity below US$1,000, or no pair data for 24 consecutive hours. From the first dead sample, price is treated as 0.
- Outage rule: when 20 or more tokens are tracked, a sampling round in which Dexscreener returns data for under 5% of them is discarded, not recorded as missing data. Any individual request that fails after retries records no sample for its tokens. (Below 20 tracked tokens, absence is ambiguous with indexing lag; the 24-hour no-data rule makes short outages harmless.)

## End of phase 1 (after at least 2 weeks and after counting graduates per day)

Decided once, then written into a phase 2 pre-registration and hashed before phase 2 starts:
- Kill-filter thresholds (may be tightened or loosened once, from phase 1 data).
- Score entry threshold.
- Phase 2 length, from the observed rate of selected tokens, targeting at least 150 selected trades.
- Comparator, cost model (actual PumpSwap fees, priority fees, rent), exit rules and the pass/fail test.

## Void conditions

A collection window is void (and rerun, not patched) if:
- Feature, filter or snapshot code changes mid-window.
- Pump.fun changes migration or pool mechanics.
- Data outage above 6 hours in total.
