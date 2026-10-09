# Robinhood Chain integration notes (chain id 4663, Arbitrum Orbit)

What we learned building the Pons collector, so the next builder doesn't have to.

## Data access
- **Blockscout** is the practical free API (`https://api.blockscout.com/v2/api?chainid=4663` for the Etherscan-style module API and `https://api.blockscout.com/4663/api/v2/` for REST). Free key from dev.blockscout.com; 5 req/s; 100k credits/day.
- **Credits are per call type, not per call** (docs: plans-and-credits, 9 Oct 2026): default 20, token transfers / token info 30, internal transactions 40, raw-trace / summary 50. `collect_rh.py::blockscout_cost` models this so the daily budget is real. A 402 means the day's credits are gone; the collector sleeps until 00:00 UTC instead of crashing.
- A big graduated token can cost 18k credits in per-tx detail lookups (300 × 60). We cap `max_graduated_tx_details` at 50; three such tokens were eating 54k of 70k credits a day before the cap.
- `getLogs` returns at most 1,000 rows; the collector bisects block ranges until a window fits.

## Pons launchpad
- Launches are `TokenLaunched` events on the factory contracts listed in `config.toml` (two candidate factories; `probe.py` reports which are live). Topics: `topic1` = token address, `topic2` = bonding-curve contract, `topic3` = deployer.
- Buys are ERC-20 transfers FROM the curve; sells are transfers TO the curve. Curve → wallet ETH payouts appear as internal transactions of the curve contract; buys paid through a router do not carry the ETH value on the transfer, which is why ETH-side P&L is approximate until router decoding is added.
- After graduation, trading moves to Uniswap v4 pools; those txs are fetched as `detail` records (capped).
- Pons applies a creator-waivable anti-snipe tax; the $18.4M, 53-launch rug operation reported by The Block (27 Sep 2026) exploited exactly that waiver with 15–25 own wallets. Bundled-launch detection (≥ 2 non-creator buys in the creation block) is the first-order fingerprint.

## Chain behaviour
- FCFS sequencer, no public mempool, no priority fee: sniping is a pure latency race, which is why "snipers" on this chain are mostly automated.
- Many user wallets are **EIP-7702** delegated accounts (code at the EOA). Treat them as wallets, not contracts, when deciding what is a hub.
- Launch activity fell from ~1,000/hour in early September 2026 to ~35–210/hour in October; the sampler adapts (it takes up to 12 per hour, fewer when fewer exist).

## Windows networking gotcha
On Windows, dual-stack hosts with dead IPv6 routes cost 21 s per AAAA attempt before IPv4 succeeds: Helius calls took 42 s and Blockscout 64 s each, for 0 KB replies. `common.py` resolves IPv4 first and caches DNS for 10 minutes; calls dropped to ~0.4 s.
