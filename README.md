# memescope — who really makes the money on memecoin launchpads

Open-source forensics for memecoin launches on **Solana (pump.fun)** and **Robinhood Chain (Pons)**. It samples new launches every hour, stores each token's complete raw on-chain history, traces the money trails of creators, early buyers and top sellers, and publishes daily metrics on insider activity: bundled launches, creator-funded snipers, coordinated dumps, wash-trading wallets, and where the realised profit actually goes.

It is research tooling and a public good. It never trades, never signs, never holds funds, and has no token.

**Live dashboard:** `docs/index.html` (GitHub Pages) — static build of the nightly analysis, no server needed · **Method:** [docs/METHOD.md](docs/METHOD.md) · **Robinhood Chain notes:** [docs/ROBINHOOD_CHAIN.md](docs/ROBINHOOD_CHAIN.md)

## Why

Launchpads print tens of thousands of tokens a day. The public narrative is "some traders are just fast/smart". The on-chain record says otherwise, but nobody publishes the numbers per launch day, per chain, with a fixed method. memescope does, so that:

- retail traders can see what share of profit goes to insiders *before* they ape in;
- launchpads, wallets and explorers can plug in a reproducible insider score;
- researchers get a clean, versioned dataset instead of screenshots.

## What the first days show (6–8 Oct 2026, 390 Solana + 109 Robinhood Chain launches)

| Metric (Solana, pump.fun) | Value |
|---|---|
| Launches with a bundled first block (≥2 non-creator wallets buying in the creation slot) | 23.6% |
| Launches with a coordinated dump (≥3 early buyers selling within 5 s) | 33.6% |
| Creators that sold their entire position | 70.8% |
| Median distinct wallets per token | 3 |
| Share of realised profit taken by insiders (creator / bundle / linked / coordinated / wash), by launch day | 59–83% |
| Independent wallets (no insider flag) with ≥1 closed position that are net profitable | 20.1% |
| Median realised P&L of an independent wallet | −0.018 SOL |

Robinhood Chain (Pons) looks different so far: 5.5% bundled launches, 15.6% coordinated dumps, 45% of creators exit fully, median 4 wallets per token. ETH flows there are approximate (see METHOD), so profit shares are not reported yet.

Every number above is recomputed nightly by `analyze.py` from the raw files, with definitions fixed in advance (`docs/METHOD.md`). Nothing is hand-curated.

## Pre-registered test: do "smart money" wallets persist?

Written down before any data was collected (7 Oct 2026, `README` decision rule, now `docs/METHOD.md §4`): remove insider wallets, rank the rest by realised profit in weeks 1–2, take the top 1%, and check in weeks 3–4 whether they beat the median wallet at 95% confidence and whether copying them with a realistic delay is net positive after costs. `analyze.py` computes it automatically once 28 launch days exist (`analysis/test_status.json`). The result is published either way.

## How it works

```
collect.py  ──► collect_sol.py  (Helius RPC: pump.fun creates via mint authority, full tx history per token)
            ──► collect_rh.py   (Blockscout API: Pons TokenLaunched logs, ERC-20 transfers, curve internal txs, tx details)
            ──► trace_sol.py / trace_rh.py  (money trails: funding back ≤3 hops, profits forward; hubs = exchanges/bridges/routers)
analyze.py  ──► analysis/{SUMMARY.md, daily_metrics.csv, tokens_*.csv, wallets_sol.csv.gz, test_status.json}
```

- **Sampling:** 12 random launches per hour per chain (≈290/day/chain), fetched once the token is ≥6 h old (most are dead by then; the history is final).
- **Raw-first:** everything is stored as gzipped JSONL exactly as returned by the chain, so definitions can change later without re-fetching. `samples/` has real examples.
- **Money trails:** creator + first 10 buyers + top 5 sellers; funding traced back up to 3 hops, profits traced forward; stops at hubs (CEX hot wallets, bridges, routers, busy wallets). Insiders use fresh wallets, but fresh wallets must be funded and profits must be collected somewhere.
- **Budgets:** runs inside the free tiers (Helius 1M credits/month; Blockscout 100k credits/day) with per-day caps and IPv4-first DNS caching (Windows IPv6 timeouts were costing 40–60 s per call before that fix).
- **Stdlib only.** No pandas, no cloud, no AI in the pipeline. A laptop runs it.

## Run it

```
setx HELIUS_API_KEY "..."          # free key: dev.helius.xyz
setx BLOCKSCOUT_API_KEY "..."      # free key: dev.blockscout.com
test.bat      # offline self-tests (fake chain data incl. a planted insider cluster) — no API calls
probe.bat     # ~30 real calls, writes samples to probe/ so you can eyeball formats
collect.bat   # leave open; one pass per hour; resumes where it stopped; STATUS.txt shows progress
analyze.bat   # rebuild analysis/ by hand (collect.py also runs it nightly after 00:10 UTC)
```

Linux/macOS: `python collect.py`, `python analyze.py`. Python ≥3.11, no dependencies.

## Roadmap

1. Public dashboard with per-token insider score and wallet lookups (this submission's demo is the static version).
2. Persistence-test result (~early Nov 2026) and a short paper with the full dataset.
3. Robinhood Chain: exact ETH flows via router decoding; coverage of post-graduation Uniswap v4 trading.
4. A tiny API (`/token/<mint>` → insider flags) for wallets, launchpads and explorers to embed.

## Honesty notes

- Four launch days of data at submission time. The headline numbers will move; the method won't.
- Robinhood Chain profit attribution is approximate until router decoding lands.
- Sampling is random, 12/hour: good for population statistics, not for looking up an arbitrary token (yet).

MIT licence. Built by Alex Chang (Sydney) with Claude.
