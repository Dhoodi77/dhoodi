# Configuration Reference

All configuration is environment variables (or `.env`). Copy
`.env.example` and fill it in. **The system refuses to trade — including
paper — until every `TRADEOS_RISK_*` parameter is set.**

## Core

| Variable | Default | Meaning |
|---|---|---|
| `TRADEOS_MODE` | `development` | `development` (no trading), `paper` (simulated), `live` (Stage 7, disabled) |
| `TRADEOS_API_HOST` / `TRADEOS_API_PORT` | `127.0.0.1` / `8420` | bind address. Put a TLS reverse proxy in front for remote/phone access |
| `TRADEOS_DASHBOARD_TOKEN` | — | required outside development. `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `TRADEOS_DATABASE_PATH` | `./data/tradeos.db` | SQLite file; `data/KILLSWITCH` next to it is the file kill switch |

## Models

| Variable | Default | Used for |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | optional; without it agents run deterministic heuristics |
| `TRADEOS_MODEL_FAST` | `claude-haiku-4-5` | classification-grade work |
| `TRADEOS_MODEL_REASONING` | `claude-opus-5` | research, financial analysis |
| `TRADEOS_MODEL_DECISION` | `claude-opus-5` | critic, executive rationale |

## Risk policy (all required, all > 0)

| Variable | Suggested | Enforced as |
|---|---|---|
| `TRADEOS_RISK_MAX_INITIAL_POSITION_USD` | 20 | per new position; **code caps at $20 regardless** |
| `TRADEOS_RISK_MAX_POSITION_USD` | 20 | position ceiling (scaling rules are future work) |
| `TRADEOS_RISK_MAX_DAILY_LOSS_USD` | 40 | realized daily loss circuit breaker → kill switch |
| `TRADEOS_RISK_MAX_PORTFOLIO_EXPOSURE_USD` | 100 | sum of open cost basis |
| `TRADEOS_RISK_MAX_SLIPPAGE_PCT` | 3.0 | per instruction |
| `TRADEOS_RISK_MAX_GAS_USD` | 2.0 | per instruction |
| `TRADEOS_RISK_MAX_OPEN_POSITIONS` | 5 | concurrent positions |
| `TRADEOS_RISK_MAX_DRAWDOWN_PCT` | 25 | vs peak equity → kill switch |
| `TRADEOS_RISK_EMERGENCY_STOP_LOSS_USD` | 80 | total-loss emergency shutdown |

Suggested values are starting points for a small paper account, not
recommendations for real money.

## Trading universe & discovery

| Variable | Default |
|---|---|
| `TRADEOS_ALLOWED_CHAINS` | `solana,ethereum,base,bsc,arbitrum,polygon` |
| `TRADEOS_ALLOWED_DEXES` | empty = any |
| `TRADEOS_MIN_LIQUIDITY_USD` | 25000 |
| `TRADEOS_MIN_VOLUME_24H_USD` | 50000 |
| `TRADEOS_MIN_TOKEN_AGE_HOURS` | 1 |
| `TRADEOS_DISCOVERY_INTERVAL_S` / `TRADEOS_MONITOR_INTERVAL_S` | 120 / 30 |

Note: "robinhood" chain from the product vision is not in the default list —
DexScreener support for it should be verified before adding it to
`TRADEOS_ALLOWED_CHAINS` (the architecture needs no code change, just the
chain id).

## Paper trading & exits

| Variable | Default |
|---|---|
| `TRADEOS_PAPER_STARTING_BALANCE_USD` | 500 |
| `TRADEOS_PAPER_SIMULATED_SLIPPAGE_PCT` | 1.0 |
| `TRADEOS_EXIT_STOP_LOSS_PCT` | 15 |
| `TRADEOS_EXIT_TAKE_PROFIT_PCT` | 30 |
| `TRADEOS_EXIT_MAX_HOLD_HOURS` | 24 |

## Helius indexer (Solana intelligence)

| Variable | Default | Meaning |
|---|---|---|
| `TRADEOS_HELIUS_API_KEY` | — | enables the Helius provider (DAS holder distribution, parsed swap history) and the smart-money scanner. Key from https://dev.helius.xyz |
| `TRADEOS_SMARTMONEY_SCAN_INTERVAL_S` | 600 | scanner cadence |
| `TRADEOS_SMARTMONEY_MAX_WALLETS_PER_SCAN` | 8 | wallets analyzed per pass (rate-limit budget) |
| `TRADEOS_SMARTMONEY_MIN_WALLET_TRADES` | 3 | completed SOL round trips required before a wallet gets scored |
| `TRADEOS_SMARTMONEY_SCORE_THRESHOLD` | 65 | effective score at which a wallet counts as smart money in token signals |
| `TRADEOS_WHALE_SOL_THRESHOLD` | 50 | SOL size at which a swap counts toward the whale flow signal |

With the key set, opportunities get real `smart_money_score` and
`whale_score` values derived from recorded swaps; without it those stay
null and scoring uses its neutral default.

## Etherscan V2 (EVM wallet intelligence)

| Variable | Default | Meaning |
|---|---|---|
| `TRADEOS_ETHERSCAN_API_KEY` | — | one free key covers ethereum, base, bsc, arbitrum, polygon (V2 unified API); enables per-chain smart-money scanners |
| `TRADEOS_WHALE_USD_THRESHOLD` | 5000 | EVM whale swap size; only stablecoin-denominated swaps count (USD-unambiguous) |

Etherscan has no parsed-swap API, so swaps are reconstructed per
transaction hash from token transfers plus normal/internal transactions,
and only unambiguous single-token-vs-counter swaps are recorded (counters:
WETH/WBNB/WPOL, USDC/USDT/DAI variants, native coin). Multi-hop routes are
skipped. Round trips match within the same counter asset, so no price feed
is needed for return percentages.

## Helius webhooks (real-time wallet tracking)

| Variable | Default | Meaning |
|---|---|---|
| `TRADEOS_PUBLIC_URL` | — | public HTTPS base URL of this instance (reverse-proxied); Helius delivers to `{PUBLIC_URL}/webhooks/helius` |
| `TRADEOS_HELIUS_WEBHOOK_SECRET` | — | shared secret; Helius echoes it in the Authorization header and the receiver rejects everything else (constant-time compare) |
| `TRADEOS_WEBHOOK_MAX_ADDRESSES` | 100 | cap on tracked wallet addresses registered with Helius |

Both must be set (in addition to the API key) or the system stays on
polling. The webhook registration self-syncs: created at startup once at
least one smart-money wallet is tracked, and the address list updates
after every scanner pass. Deliveries feed the same pipeline with the same
risk gates — a webhook can accelerate analysis, never bypass anything.

Real-time reactions:
- tracked smart-money wallet **buys** → high alert + the token enters the
  opportunity pipeline immediately
- tracked wallet **sells a token you hold** → critical alert (position
  requires attention)

## Other optional integrations (inactive until set)

| Variable | Activates |
|---|---|
| `TRADEOS_RPC_SOLANA`, `TRADEOS_RPC_ETHEREUM`, `TRADEOS_RPC_BASE`, `TRADEOS_RPC_BSC`, `TRADEOS_RPC_ARBITRUM`, `TRADEOS_RPC_POLYGON` | on-chain agent holder/transfer analysis (`TRADEOS_RPC_SOLANA` is ignored when the Helius key is set — Helius provides the RPC) |
| `TRADEOS_SEARCH_API_KEY` | web agent |
| `TRADEOS_X_BEARER_TOKEN`, `TRADEOS_REDDIT_CLIENT_ID`/`_SECRET` | social monitoring |

## Live trading gate (Stage 7)

`TRADEOS_MODE=live` additionally requires
`TRADEOS_LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS`; even then the live
engine currently fails closed — see docs/OPERATIONS.md for the full
checklist that must exist before live execution is implemented.
