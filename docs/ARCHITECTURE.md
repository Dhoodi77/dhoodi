# TradeOS Architecture

## Design principle

Two layers with a one-way trust relationship:

1. **Reasoning layer** (agents, LLM calls): produces analysis, verdicts,
   and decision objects. Assumed fallible and untrusted.
2. **Deterministic layer** (risk engine, execution gateway, kill switch,
   validators, exit rules): plain code. Every trade — entry or exit, paper
   or live — passes through it. Nothing in the reasoning layer can widen a
   limit, skip a check, or execute directly.

## Component map

```
src/tradeos/
├── config.py            all financial parameters; hard $20 cap constant
├── logging_setup.py     JSON logs, correlation ids, secret redaction
├── db/                  SQLite (WAL), full schema, audit log
├── llm/                 Anthropic client, role→model routing, JSON parsing
├── agents/              9 agents (reasoning layer)
├── orchestration/       pipeline, position monitor, scheduler, recovery
├── providers/           DexScreener + EVM/Solana RPC + Helius indexer
├── strategies/          momentum engine, versioned opportunity scoring
├── risk/                policy, deterministic engine, kill switch
├── execution/           instruction model, gateway, paper engine, live stub
├── portfolio/           cash ledger, positions, P&L, drawdown state
├── wallets/             smart-money reputation, round-trip analysis, scanner
├── memory/              user/market/trading/agent memory layers
├── learning/            post-trade reviews, versioned strategies
├── api/                 FastAPI server, auth, control endpoints
└── web/static/          mobile-first dashboard (vanilla JS)
```

## Decision pipeline

```
DexScreener discovery ──► snapshot stored ──► dedup / chain filter
      ──► deterministic scoring (momentum, safety red flags, risk score)
      ──► prefilter (obvious unfit rejected before agents run)
      ──► Research ─► On-chain ─► Financial ─► Risk agent (adaptive
          confirmations, veto) ─► Critic (tries to disprove)
      ──► Executive (combination rules; LLM writes rationale only)
      ──► TradeInstruction built by deterministic code ($20-capped sizing)
      ──► ExecutionGateway: kill switch → pydantic validation → RiskEngine
          (policy, circuit breakers, per-trade limits)
      ──► Paper/Live engine ──► position ──► monitor loop (stop-loss /
          take-profit / max-hold) ──► exit via the same gateway
      ──► post-trade review stored ──► performance record
```

Every step writes to `audit_log` under one correlation id (`opp_…`), so a
trade is fully reconstructable: snapshot → verdicts → risk events → trades
→ position → review.

## Executive combination rules (deterministic)

- Risk agent `reject` → vetoed, final.
- Critic `reject` → rejected; `needs_evidence` → investigate.
- Otherwise: independent buy/approve confirmations must reach the risk
  agent's adaptive requirement (1–4, scaled by token age, liquidity,
  volatility, concentration).
- The LLM may only phrase the rationale, never flip the outcome.

## Model routing

| Role | Default | Used by |
|---|---|---|
| fast | claude-haiku-4-5 | on-chain, risk, planning, web |
| reasoning | claude-opus-5 | research, financial, coding, post-trade critic |
| decision | claude-opus-5 | critic, executive |

Configurable via `TRADEOS_MODEL_*`. With no API key the agents run pure
heuristics — the pipeline works end to end without an LLM.

## Smart-money discovery (Helius, Solana)

With `TRADEOS_HELIUS_API_KEY` set, a scanner loop runs every
`TRADEOS_SMARTMONEY_SCAN_INTERVAL_S`:

```
tokens the system watches (open positions + recent opportunities)
  ─► Helius parsed swap history per token (SOL<->token swaps only;
     ambiguous routes skipped, never approximated)
  ─► active wallets extracted, stale-scored ones re-queued
  ─► per-wallet swap history ─► SOL round trips (average cost basis;
     sells with no observed buy are ignored — conservative by design)
  ─► WalletPerformance ─► smart-money score (sample-size confidence,
     14-day decay toward neutral) ─► wallet_scores
```

Recorded swaps also produce two token-level signals consumed by the
scoring engine and the critic:

- **smart-money score**: 50 baseline, +15 per distinct smart-money buyer
  in 24h, −10 per smart-money seller; null when no scored wallet touched
  the token (never faked).
- **whale score**: buy/sell balance of swaps ≥ `TRADEOS_WHALE_SOL_THRESHOLD`
  SOL; null when no whale-sized flow. Whale selling (< 40) becomes a
  critic objection — a whale transaction is never assumed bullish.

Holder distribution for Solana upgrades from the RPC top-20 approximation
to full DAS `getTokenAccounts` pagination (top-10/top-20 concentration,
holder count), feeding the on-chain agent's concentration checks.

## Security model

- **Hard cap**: `HARD_INITIAL_TRADE_CAP_USD = 20` in code; config can
  lower, never raise. Enforced at policy construction AND per trade.
- **Fail closed**: missing risk parameters, unknown chain, zero price,
  missing wallet → refusal, not a default.
- **Kill switch**: DB flag + filesystem sentinel checked in the gateway
  before anything else; circuit-breaker trips activate it automatically.
  While active, entries AND autonomous exits halt (positions preserved per
  policy) and exit failures raise critical alerts.
- **Secrets**: never in code, DB, memory layers, or logs (redaction filter
  for key shapes runs on every log line and memory write). Private keys
  never enter this process — live signing is designed to be external.
- **Auth**: constant-time bearer token, failure rate limiting, no docs
  endpoints, unauthenticated surface limited to `/api/health`.
- **LLM boundary**: agent output is parsed into typed verdicts; malformed
  output degrades to heuristics; instructions are pydantic-validated and
  re-checked by the risk engine.

## Failure & recovery

- Scheduler loops catch all exceptions → system_event + retry next tick.
- Provider calls: bounded retries with backoff, rate limiters, then `None`
  (degrade) — never fabricated data.
- Startup: pending trades are flagged for reconciliation with a critical
  alert; open positions re-verified against live prices by the monitor;
  portfolio state reconstructs entirely from the database (tested).
- Process supervision via systemd (`scripts/tradeos.service`), WAL-mode
  SQLite for crash safety.

## Deliberate omissions (not oversights)

- **Live execution** fails closed until Stage 7: wallet signer isolation,
  per-chain RPC config, DEX router integration (Jupiter / 0x-style), and
  pre-submit transaction simulation are prerequisites.
- **Coding agent** is proposal-only: an agent that can edit trading logic
  can edit its own risk limits.
- **Web agent** stays inert without credentialed APIs rather than scraping
  in violation of ToS or hallucinating social sentiment.
