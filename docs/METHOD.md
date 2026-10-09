# Method (fixed in advance; versioned with `analyze.py` `VERSION`)

## 1. Sampling
- **Solana / pump.fun:** every token created through the pump.fun program is signed by the mint authority `TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM`. The collector pages that account's signatures per hour, keeps the create transactions, and samples 12 at random per launch hour (`sample_per_hour`).
- **Robinhood Chain / Pons:** `TokenLaunched` logs from the Pons factory contracts (`config.toml [robinhood].factories`; topic1 = token, topic2 = bonding curve, topic3 = deployer, verified with `probe.py`). 12 random launches per hour.
- A token is fetched once it is ≥ 6 h old (`min_age_hours`) and never older than 30 h at sampling (`max_age_hours`). Histories are capped at 3,000 txs (Solana) / 5,000 transfers (Robinhood Chain); capped tokens are flagged `truncated` and the EARLIEST activity is kept.

## 2. Raw storage (never modified)
`data/raw_<chain>/<YYYYMMDD>/<token>.jsonl.gz`, one JSON object per line: a `meta` line, then every transaction / transfer / internal transfer / tx detail exactly as returned by Helius or Blockscout. Traces live in `data/raw_<chain>_trace/`. See `samples/` for real files.

## 3. Definitions (Solana unless noted)
- **trade:** a successful tx whose fee payer's token balance in the mint changes; `d_sol` = payer lamport change + payer WSOL change (fees included).
- **creation slot:** slot of the token's first recorded tx.
- **bundled launch:** ≥ 2 non-creator wallets buy in the creation slot (same block on Robinhood Chain).
- **sniper:** a non-creator wallet whose first buy is within 2 slots (2 blocks) of creation.
- **coordinated dump:** ≥ 3 distinct wallets among the first 20 buyers sell within the same 5-second window.
- **wash / volume-bot wallet:** ≥ 10 trades in the token, both buys and sells, |net tokens| ≤ 1 % of tokens bought and |net SOL| ≤ 2 % of its SOL volume.
- **creator-linked wallet** (traced tokens only): funded by the creator, or shares a non-hub funder with the creator within 3 hops.
- **insider** (for the persistence test): creator, creation-slot buyer, creator-linked, coordinated-dump member or wash wallet in ANY sampled token.
- **realised P&L:** sum of `d_sol` for wallets whose token position is closed (sold ≥ 99 % of tokens bought). Open positions are excluded, so "win rate" cannot be inflated by never closing losers.
- **profit shares:** positive realised P&L of each role ÷ total positive realised P&L on that launch day.
- **Robinhood Chain:** token flows are exact (ERC-20 transfers); ETH flows use direct curve payouts and fetched tx values only, so P&L is marked `approx` and is excluded from profit shares and from the persistence test until router decoding is added.

## 4. Pre-registered persistence test (written 7 Oct 2026, before data)
Computed by `analyze.py` once ≥ 28 full launch days exist; published in `analysis/test_status.json` whatever the result.
1. Remove insider wallets (definition above).
2. Rank the remaining wallets by realised P&L over launch weeks 1–2; take the top 1 % (minimum 20 wallets).
3. In launch weeks 3–4 that group must beat the median wallet's P&L at 95 % confidence (one-sided bootstrap).
4. A copy simulation that follows those wallets with a realistic delay (Solana ≥ 1 slot + 400 ms; Robinhood Chain ≥ 1 s), plus slippage and fees, must be net positive.
Both pass → there is a learnable edge and we study what those wallets do. Either fails → "follow smart money" is dead on these venues and we say so.

## 5. Money trails
For ~35 % of tokens at random plus every token where one wallet took out ≥ 5 SOL / ≥ 1 ETH: creator + first 10 buyers + top 5 sellers. Backward: the funder of each wallet, up to 3 hops, stopping at hubs (wallets with > 2,000 txs on Solana / > 20,000 on Robinhood Chain, known exchanges, bridges, routers). Forward: the newest outgoing transfers of each seller and of the creator. Results are cached per wallet in `state_<chain>.sqlite` so a wallet is traced once.

## 6. Limits
- 12 launches/hour is a population sample, not full coverage.
- Hubs break trails: funding via a CEX hot wallet is recorded as "hub", not resolved.
- Bonding-curve-only history on Solana; post-graduation DEX trading is included only where the token's own account is touched.
- Robinhood Chain wallets are frequently EIP-7702 delegated; they are treated as normal wallets.
