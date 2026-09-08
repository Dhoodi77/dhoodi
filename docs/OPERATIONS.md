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

## Live trading checklist (Stage 7 — all required before implementing)

1. Paper mode has run for a meaningful period with reviewed post-trade
   results.
2. External signing: keys held by a separate signer service or hardware
   wallet; this process must never see key material.
3. Trading wallets funded with only what they may lose, isolated from
   treasury.
4. Per-chain RPC endpoints configured and failover tested.
5. DEX router integration (e.g. Jupiter for Solana; 0x/1inch for EVM) with
   pre-submit transaction simulation.
6. All risk parameters reviewed; `TRADEOS_LIVE_TRADING_CONFIRM` set.
7. Kill switch drill performed (all three activation paths).
