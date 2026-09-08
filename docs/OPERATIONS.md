# Operations

## VPS deployment

```bash
sudo useradd -r -m -d /opt/tradeos tradeos
sudo -u tradeos git clone <repo> /opt/tradeos
cd /opt/tradeos
sudo -u tradeos pip3 install .
sudo -u tradeos cp .env.example .env   # then edit: token + risk params
sudo -u tradeos chmod 600 .env
sudo cp scripts/tradeos.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tradeos
```

systemd restarts the process on any crash (`Restart=always`). The network
must allow egress to `api.dexscreener.com`, `api.anthropic.com`, and any
configured RPC endpoints.

### Phone access

Do not expose port 8420 directly. Put nginx/caddy with TLS in front, or use
a WireGuard/Tailscale tunnel to the VPS and open `http://<vps>:8420` in the
iPhone browser (Add to Home Screen works — the UI is standalone-friendly).
The dashboard token is stored in the phone browser's localStorage after
first login.

## Health & observability

- `GET /api/health` — unauthenticated liveness (for uptime monitors).
- Dashboard "System"/"Agents" cards — trading state, loop heartbeats,
  agent heartbeats.
- Logs: single-line JSON on stdout (journald) with correlation ids; every
  opportunity traceable via `/api/audit?opportunity_id=opp_…`.
- Tables `system_events`, `risk_events`, `alerts` hold the operational
  record.

## Kill switch

Activate (any of):
1. Dashboard red button
2. `curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8420/api/kill-switch/activate`
3. `touch /opt/tradeos/data/KILLSWITCH` (works even if the API is down)

Effect: all execution (entries and autonomous exits) halts; positions are
preserved; a critical alert and audit event are recorded. Circuit breakers
(daily loss, drawdown, emergency stop) trip it automatically.

Resume: `POST /api/kill-switch/resume` — refused unless risk policy is
complete, circuit breakers pass on current portfolio state, and the
`KILLSWITCH` file (if used) has been removed manually.

## Restart / recovery behavior

On startup the system reconstructs portfolio state from SQLite, flags any
`pending` trades for manual reconciliation (critical alert — never assumes
an interrupted process meant an interrupted trade), and the monitor
re-verifies open positions against live prices before any new decisions.

## Backups

Everything lives in `data/tradeos.db`. Snapshot it (e.g. nightly
`sqlite3 data/tradeos.db ".backup backup-$(date +%F).db"`). The `.env` file
holds secrets — back it up separately and encrypted, never into git.

## Live trading (Stage 7 — Solana via Jupiter)

The live engine exists and fails closed until every prerequisite is met.
`GET /api/live/readiness` reports exactly what is missing, including live
health checks of the signer, venue, and RPC. EVM live execution is not
implemented and refuses explicitly.

Enable in this order — do not skip steps:

1. **Paper first.** Run paper mode long enough to have reviewed post-trade
   results and `/api/signals/performance` data you actually believe.
2. **Signer.** Deploy `signer/` as a separate OS user (ideally separate
   host) per `signer/README.md`. Fund its wallet with only what it may
   lose. Set `TRADEOS_SIGNER_URL` + `TRADEOS_SIGNER_TOKEN` in TradeOS.
3. **Register the wallet** (address only — never a key):
   `POST /api/wallets {"label":"main","chain":"solana","address":"...","kind":"trading"}`.
   Treasury wallets get `"kind":"treasury"` — execution refuses them.
4. **RPC**: `TRADEOS_HELIUS_API_KEY` (recommended) or `TRADEOS_RPC_SOLANA`.
5. **Review risk parameters**, then set `TRADEOS_MODE=live` and
   `TRADEOS_LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS`.
6. **Verify** `GET /api/live/readiness` returns `ready: true`.
7. **Drill the kill switches**: dashboard button, `touch data/KILLSWITCH`,
   and `touch signer/SIGNER_KILLSWITCH` (the signer-side stop works even if
   TradeOS itself is compromised).

Per-trade flow (all deterministic): risk engine → Jupiter quote →
slippage/price-impact/mint validation → priority-fee cap vs max_gas →
RPC simulation (must pass) → pending trade row → external signer (own
policy) → submit → confirmation polling → fill from on-chain balance
deltas (quote estimate as audited fallback). Unconfirmed trades stay
`pending` and are reconciled by the monitor loop — never assumed failed,
never assumed filled.
