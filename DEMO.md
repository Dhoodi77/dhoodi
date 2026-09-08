# TradeOS Local Demo — Complete Guide

🟢 **DEMO MODE — PAPER TRADING ONLY**

No real money. No blockchain transactions. No private keys. Safe to explore and test.

---

## Quick Start (60 seconds)

### 1. Start Demo

```bash
bash scripts/demo.sh
```

That's it. The demo starts with:
- **Mode:** Paper trading ($10,000 starting balance)
- **Dashboard:** http://localhost:8420
- **Token:** `demo_token_tradeos_2024`
- **Agents:** All 9 agents running
- **Market Data:** Live (from DexScreener)
- **Trades:** Simulated only

### 2. Access Dashboard

**On your computer:**
```
http://localhost:8420
```

**On your iPhone (same Wi-Fi):**
```
http://<YOUR_IP>:8420
```

To find `<YOUR_IP>`:
```bash
bash scripts/get-local-ip.sh
```

Your iPhone will show:
```
http://192.168.x.x:8420
```

### 3. Authenticate

Token: `demo_token_tradeos_2024`

---

## What You'll See

### Portfolio Dashboard

**🟢 DEMO / PAPER TRADING** badge at top right

**Stats:**
- Equity: $10,000 (start)
- Cash: Decreases as trades open
- Exposure: Amount invested in open positions
- Daily P&L: Realized profit/loss since midnight UTC
- Total P&L: All time realized + unrealized

**Charts:**
- 48-hour equity curve (starts flat)
- Real market data (live from DexScreener)
- Simulated trades execute against real prices

### Agent Roster

9 agents with status indicators:

1. **Xavi** (Executive) — Main coordinator
2. **Messi** (Research) — Token analysis
3. **Ronny** (On-Chain) — Blockchain intel
4. **Iniesta** (Financial) — Metrics analysis
5. **Neymar** (Risk) — Risk evaluation
6. **Suarez** (Critic) — Decision review
7. **Maradona** (Planning) — Strategy
8. **Pele** (Web) — Web research
9. **Ronaldinho** (Coding) — Code analysis

Green dot = agent online. Gray dot = offline.

### Trading Pipeline

**Opportunities → Analysis → Decisions → Paper Execution → Positions**

1. **Discovery:** System finds tokens (120s interval)
2. **Analysis:** Agents analyze each token
3. **Scoring:** Overall score (1-100)
4. **Execution:** If approved, paper trade opens
5. **Monitoring:** Positions tracked in real-time

### Open Positions

Shows:
- Entry price
- Current price (live market)
- Quantity
- P&L (marked-to-market)
- Stop-loss / take-profit rules

Click "Close" to manually exit (fills at current market price minus slippage).

### Risk Dashboard

Enforced limits:
- **Max initial trade:** $20 (hard code limit)
- **Max daily loss:** $40
- **Max portfolio exposure:** $100
- **Max open positions:** 5
- **Max slippage:** 3%
- **Kill switch:** Instant stop if activated

---

## Demo Controls

### Dashboard Controls

**Stop Agents** (top right)
```
POST /api/demo/agents/pause
```
Pauses all 9 agents mid-cycle.

**Resume Agents**
```
POST /api/demo/agents/start
```
Resumes agent execution.

**Kill Switch** (red button)
Stops ALL trading immediately. Click "Resume" to re-enable.

**Reset Portfolio** (bottom right)
```
POST /api/demo/reset
```
Clears all trades/positions. Resets to $10,000.

⚠️ **WARNING:** Reset is destructive. All history lost.

---

## Testing Scenarios

### 1. Verify $20 Trade Limit

1. Dashboard loads with $10,000
2. Monitor opportunities (wait 2 minutes for discovery)
3. Watch agents analyze tokens
4. First approved trade should execute at ≤ $20
5. Try forcing a $21 trade via API → Rejected ✓

```bash
# Check risk engine rejects oversized trade:
curl -H "Authorization: Bearer demo_token_tradeos_2024" \
  http://localhost:8420/api/dashboard
# Should show risk_engine refusing 21-dollar trade
```

### 2. Verify $3 Stop-Loss

1. Open a $10 position
2. Wait for position to move
3. Set stop-loss trigger at -$3
4. Position auto-closes when unrealized loss hits -$3 ✓

### 3. Verify Position Limits

1. Execute 5 trades (each ~$20)
2. 6th trade rejected → "max open positions" error ✓
3. Close 1 position
4. 6th trade now accepted ✓

### 4. Verify Kill Switch

1. Click red "Kill Switch" button
2. All new trades rejected
3. Error message: "kill switch active"
4. Click "Resume"
5. Trades resume ✓

### 5. Verify Reset

1. Open multiple positions
2. Click "Reset Portfolio"
3. All positions closed
4. Balance reset to $10,000
5. History cleared ✓

### 6. Verify No Real Signing

Demo mode blocks all real signing:
- No private keys loaded
- No signer service connected
- No blockchain transactions possible
- API returns "live execution requires async path" error if mode != paper

---

## API Endpoints

### Read (All require auth token)

```bash
TOKEN="demo_token_tradeos_2024"

# Dashboard summary
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/dashboard

# Comprehensive desk view
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/desk

# Open positions
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/positions?status=open

# Closed positions  
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/positions?status=closed

# Opportunities
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/opportunities

# Trades
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/trades

# Audit log
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/audit
```

### Demo Controls

```bash
TOKEN="demo_token_tradeos_2024"

# Get demo status
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/demo/status

# Pause agents
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/demo/agents/pause

# Resume agents
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/demo/agents/start

# Get agent statuses
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/demo/agents/status

# Kill switch: activate
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"testing"}' \
  http://localhost:8420/api/kill-switch/activate

# Kill switch: resume
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/kill-switch/resume

# Reset paper trades
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8420/api/demo/reset

# Health check (no auth required)
curl http://localhost:8420/api/health
```

---

## Database

Demo uses SQLite for simplicity:
```
./data/tradeos_demo.db
```

View with:
```bash
sqlite3 ./data/tradeos_demo.db
```

Check recent trades:
```sql
SELECT id, symbol, side, cost_usd, status, filled_at 
FROM trades WHERE mode = 'paper' ORDER BY created_at DESC LIMIT 20;
```

Check open positions:
```sql
SELECT id, symbol, cost_usd, quantity, entry_price_usd, status 
FROM positions WHERE status = 'open' AND mode = 'paper';
```

---

## Configuration

Demo configuration in `.env.demo`:

Key settings:
```bash
TRADEOS_MODE=paper
TRADEOS_DEMO_MODE=true
TRADEOS_DEMO_STARTING_BALANCE=10000.0
TRADEOS_API_HOST=0.0.0.0
TRADEOS_DASHBOARD_TOKEN=demo_token_tradeos_2024
```

Risk limits (code-enforced):
```bash
TRADEOS_RISK_MAX_INITIAL_POSITION_USD=20.0
TRADEOS_RISK_MAX_DAILY_LOSS_USD=40.0
TRADEOS_RISK_MAX_PORTFOLIO_EXPOSURE_USD=100.0
TRADEOS_RISK_MAX_OPEN_POSITIONS=5
```

Exit rules:
```bash
TRADEOS_EXIT_STOP_LOSS_PCT=15.0
TRADEOS_EXIT_TAKE_PROFIT_PCT=30.0
TRADEOS_EXIT_MAX_HOLD_HOURS=24.0
```

---

## Logs

View real-time logs:
```bash
tail -f logs/tradeos.log
```

View Docker logs (if using):
```bash
docker-compose logs -f tradeos
```

---

## Stop Demo

Press `Ctrl+C` in the terminal, or:

```bash
bash scripts/demo-stop.sh
```

---

## Troubleshooting

### "Connection refused" on iPhone

1. Check local IP:
   ```bash
   bash scripts/get-local-ip.sh
   ```
2. Verify both devices on same Wi-Fi
3. Try exact IP + port: `http://192.168.x.x:8420`
4. Check firewall allows port 8420

### "No opportunities discovered"

Agents need 2-3 minutes to bootstrap. Wait for:
- Opportunity discovery (120s interval)
- Token analysis (varies)
- First trade usually appears at ~3 minutes

Check logs: `tail -f logs/tradeos.log | grep -i discovery`

### "Risk engine refused trade"

Check dashboard for violations:
- Exceeds $20 single trade? ✓ Rejected
- Exceeds $40 daily loss? ✓ Rejected
- Exceeds $100 exposure? ✓ Rejected
- Exceeds 5 open positions? ✓ Rejected
- Kill switch active? ✓ Rejected

All rejections logged in audit trail.

### "Agents offline / not analyzing"

1. Check scheduler running:
   ```bash
   curl -H "Authorization: Bearer demo_token_tradeos_2024" \
     http://localhost:8420/api/demo/agents/status
   ```

2. If all agents offline, restart:
   ```bash
   curl -X POST -H "Authorization: Bearer demo_token_tradeos_2024" \
     http://localhost:8420/api/demo/agents/start
   ```

3. Check API logs for errors:
   ```bash
   tail -f logs/tradeos.log | grep -i error
   ```

### "Market data not updating"

DexScreener may be rate-limited or unreachable:
```bash
curl -s "https://api.dexscreener.com/api/tokens/solana/EPjFWdd5Au" | jq .pairs[0].price
```

If no response, DexScreener is down. Demo waits and retries automatically.

---

## Safety Guarantees

✓ **No private keys:** Never loaded, never logged, never transmitted  
✓ **No real signing:** Signer service not connected  
✓ **No blockchain:** All trades simulated  
✓ **No real funds:** Only $10,000 paper money  
✓ **Kill switch:** Stops everything instantly  
✓ **Risk limits:** Code-enforced, cannot be bypassed  
✓ **Audit trail:** Every trade logged to database  

---

## Next Steps

After exploring the demo:

1. **Read PRODUCTION_CHECKLIST.md** if you want live trading (NOT recommended without deep testing)
2. **Review SECURITY.md** to understand security boundaries
3. **Read DEPLOYMENT.md** for production VPS setup
4. **Study the codebase** in `src/tradeos/` to understand how everything works

---

## Questions?

Check logs for detailed debugging info. Every action is logged to:
- `logs/tradeos.log` — Application log
- `data/tradeos_demo.db` — SQLite database (all data)
- API audit trail — `/api/audit` endpoint

Good luck! 🚀
