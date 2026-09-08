# TradeOS

Personal autonomous crypto intelligence and trading system. Multi-agent AI
analysis (Claude) on top of a fully deterministic risk and execution core.

**AI reasoning is never the security boundary.** Hard risk limits,
transaction validation, circuit breakers, and the kill switch are plain
code, enforced regardless of what any agent says.

## Status — honest summary

| Component | State |
|---|---|
| Configuration, database, structured logging, audit trail | working, tested |
| Deterministic risk engine ($20 hard cap, daily loss, drawdown, exposure, slippage, gas, position limits) | working, tested |
| Kill switch (DB flag + `KILLSWITCH` file sentinel, outside the AI layer) | working, tested |
| Paper trading engine + portfolio accounting + P&L | working, tested |
| Momentum engine + versioned opportunity scoring + red-flag scan | working, tested |
| 9-agent pipeline (research, on-chain, financial, risk, critic, executive, planning, web, coding) | working; LLM-backed with deterministic fallback |
| DexScreener provider (discovery, pairs, rate limiting, retries) | implemented; needs open egress to `api.dexscreener.com` |
| Chain providers (EVM JSON-RPC + Solana RPC) | implemented; need `TRADEOS_RPC_<CHAIN>` endpoints |
| Helius indexer (holder distribution, parsed swap history, rate limiting) | implemented, fixture-tested; needs `TRADEOS_HELIUS_API_KEY` + open egress |
| Smart-money discovery (wallet scan → round-trip P&L → score → decay) | working, tested end to end against fixtures |
| Token smart-money / whale flow signals feeding opportunity scoring and the critic | working, tested |
| Helius webhooks: real-time tracked-wallet events (self-registering, secret-authenticated) | implemented, tested; needs `TRADEOS_PUBLIC_URL` + webhook secret |
| Smart-money discovery flywheel (tokens smart wallets buy enter the pipeline) | working, tested |
| Etherscan V2 EVM wallet intelligence (5 chains, one key; deterministic swap reconstruction) | implemented, fixture-tested; needs `TRADEOS_ETHERSCAN_API_KEY` |
| Provider health checks at startup (DexScreener + Helius shape validation) | working, tested |
| Signal-outcome tracking (`/api/signals/performance` win rates per signal bucket) | working, tested |
| Position monitor (stop-loss / take-profit / max-hold) + post-trade reviews | working, tested |
| Mobile-first web dashboard + control API (token auth, rate limiting) | working, smoke-tested |
| Web agent (X/Reddit/search) | scaffold; requires API credentials, fails honest instead of fabricating |
| Live execution | **deliberately not implemented** — fails closed, audited (Stage 7) |

## Quickstart

```bash
pip install -e .[dev]
cp .env.example .env         # fill in TRADEOS_DASHBOARD_TOKEN and risk params
python3 -m pytest tests/     # 66 tests
python3 -m tradeos.main      # serves dashboard at http://127.0.0.1:8420
```

The system refuses to trade until **every** risk parameter is configured.
Modes: `development` (no trading), `paper` (simulated fills, full pipeline),
`live` (disabled until Stage 7 and explicit confirmation).

## Emergency stop

- Dashboard: red KILL SWITCH button
- API: `POST /api/kill-switch/activate`
- Shell (works even if the API is down): `touch data/KILLSWITCH`

Resume requires passing safety checks and, if the file sentinel was used,
manually removing it.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, decision pipeline, security model
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every parameter
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — VPS deployment, supervision, recovery, live-trading checklist

## License

See [LICENSE](LICENSE).
