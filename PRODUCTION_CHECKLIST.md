# TradeOS Production Readiness Checklist

Use this checklist before going live with real capital.

## Infrastructure Setup

- [ ] VPS provisioned (Ubuntu 22.04+, 2+ cores, 4+ GB RAM, 50+ GB storage)
- [ ] Static public IP assigned
- [ ] Domain name registered and pointing to VPS IP
- [ ] DNS propagated (verified with `nslookup`)
- [ ] Docker installed and working
- [ ] Docker Compose installed and working

## Configuration

- [ ] `.env` created from template
- [ ] All `[REQUIRED]` fields in `.env` filled in
- [ ] Tokens generated securely
- [ ] Database password set
- [ ] Domain name configured
- [ ] `.env` permissions set to 600 (readable only by owner)
- [ ] `.env` NOT committed to git
- [ ] `.gitignore` includes `.env` and `signer_keys/`

## SSL/HTTPS

- [ ] SSL certificates generated or obtained
- [ ] HTTPS working on dashboard
- [ ] HTTP redirects to HTTPS
- [ ] Certificate will not expire soon (or auto-renewal configured)

## Deployment

- [ ] `bash scripts/deploy.sh` completed successfully
- [ ] All Docker containers running: `docker-compose ps`
- [ ] No crashed containers
- [ ] Logs show no errors: `docker-compose logs`

## API & Dashboard

- [ ] Dashboard loads: `https://your-domain/`
- [ ] Authentication required (401 without token)
- [ ] Authentication works with correct token
- [ ] API health endpoint responds: `/health` → 200 OK
- [ ] `/api/desk` endpoint returns system state

## Database

- [ ] PostgreSQL running: `docker-compose ps | grep postgres`
- [ ] Database connection works: `docker-compose exec postgres psql -U tradeos -d tradeos -c "SELECT 1;"`
- [ ] Backup script works: `bash scripts/backup.sh`
- [ ] Restore script works: `bash scripts/restore.sh`

## Agents & Pipeline

- [ ] All 9 agents shown in roster (Xavi, Messi, Ronny, Iniesta, Neymar, Suarez, Maradona, Pele, Ronaldinho)
- [ ] Agent status indicators working
- [ ] Decision pipeline diagram rendering
- [ ] Activity log showing events

## Paper Trading (MUST TEST 24+ HOURS)

- [ ] Paper mode enabled: `grep TRADEOS_MODE .env` shows `paper`
- [ ] Starting balance correct: shows $500 or configured amount
- [ ] Portfolio accounting working (equity updates)
- [ ] Risk parameters loaded correctly
- [ ] No trades rejected due to config errors

### Paper Trading: Entry

- [ ] Opportunity discovery running
- [ ] Opportunities appearing in dashboard
- [ ] Research agent analyzing opportunities
- [ ] Risk checks passing for valid trades
- [ ] Test trade placed at $10 → ACCEPTED
- [ ] Test trade placed at $25 → REJECTED (exceeds $20 limit)

### Paper Trading: Positions

- [ ] Position created after successful entry
- [ ] Entry price recorded
- [ ] Position tracking working (unrealized P&L updating)
- [ ] Position visible in positions table

### Paper Trading: Exits

- [ ] Position exits correctly on stop-loss trigger
- [ ] Position exits correctly on take-profit trigger
- [ ] Position exits correctly on max hold time
- [ ] Realized P&L calculated correctly
- [ ] Post-trade analysis running

### Paper Trading: Risk

- [ ] Daily P&L tracking (refreshes at UTC midnight)
- [ ] Stop-loss enforcement ($3 per trade)
- [ ] Portfolio exposure limits enforced
- [ ] Maximum open positions enforced
- [ ] Drawdown tracking working

## Kill Switch

- [ ] Kill switch button visible in dashboard
- [ ] Clicking kill switch stops trading: `curl -X POST /api/kill-switch/activate`
- [ ] New trades rejected while kill switch active
- [ ] Resume button appears when switch is active
- [ ] Resume works: `curl -X POST /api/kill-switch/resume`
- [ ] Signer kill switch documented: `touch signer/SIGNER_KILLSWITCH`

## Monitoring

- [ ] Health check script works: `bash scripts/health-check.sh`
- [ ] All services show ✓ (passed)
- [ ] Resource usage reasonable (< 80% memory, < 80% disk)
- [ ] No error spam in logs
- [ ] Cron job configured for daily backups: `crontab -l | grep backup`

## Security

- [ ] Firewall configured (22, 80, 443 only from outside)
- [ ] No plaintext secrets in logs
- [ ] No passwords in git history
- [ ] SSH key-based auth (no password login)
- [ ] Regular security updates scheduled
- [ ] Off-site backups location configured

## Live Trading Prerequisites (ONLY IF ENABLING LIVE)

- [ ] Paper trading successfully run for 24+ hours
- [ ] All risks understood and accepted
- [ ] Dedicated trading wallet created
- [ ] Wallet funded with SMALL amount ($50-100 for testing)
- [ ] Wallet keys placed in `signer_keys/`
- [ ] RPC endpoints configured in `.env`
- [ ] Anthropic API key added (for better decisions)
- [ ] Live mode test readiness checked: `curl /api/live/readiness`

### Live Trading: Configuration

- [ ] `TRADEOS_MODE=live` (changed from `paper`)
- [ ] `TRADEOS_LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS`
- [ ] Signer service running: `docker-compose ps | grep signer`
- [ ] Signer can sign transactions (test endpoint responds)
- [ ] Price impact limit set: `TRADEOS_LIVE_MAX_PRICE_IMPACT_PCT=2.0`
- [ ] Gas limit configured appropriately

### Live Trading: First Trade

- [ ] Kill switch tested one more time
- [ ] Monitoring active (watch logs: `docker-compose logs -f`)
- [ ] Dashboard open and responsive
- [ ] First real trade is SMALL ($5-10)
- [ ] Trade executes successfully
- [ ] Transaction visible on blockchain explorer
- [ ] Position created correctly in dashboard
- [ ] Can close position via dashboard

## Documentation

- [ ] DEPLOYMENT.md read and understood
- [ ] PRODUCTION_CHECKLIST.md (this document) reviewed
- [ ] All scripts (`backup.sh`, `restore.sh`, `health-check.sh`) executable
- [ ] Recovery procedures documented
- [ ] Team members trained (if applicable)

## Final Verification

### Before Paper Trading
```bash
docker-compose ps                    # All services running
bash scripts/health-check.sh         # All checks pass
docker-compose logs | grep -i error  # No errors
```

### Before Live Trading
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8420/api/desk
# Response shows: "trading_allowed": false, "mode": "live"
curl -H "Authorization: Bearer $TOKEN" http://localhost:8420/api/live/readiness
# Response shows: "ready": true (if all prerequisites met)
```

## Go/No-Go Decision

**Go** ✅ if:
- [ ] All infrastructure checks pass
- [ ] Paper trading runs for 24+ hours without errors
- [ ] All risk limits enforced (verified by testing)
- [ ] Kill switch confirmed working
- [ ] Monitoring and backups operational
- [ ] Team understands procedures
- [ ] Decision made with clear head (not rushed)

**No-Go** ❌ if:
- [ ] Any security concern unresolved
- [ ] Any risk limit not enforced
- [ ] Kill switch not working
- [ ] Paper trading fails or shows inconsistencies
- [ ] Monitoring gaps exist
- [ ] Team has doubts or questions

---

## Sign-Off

Completed by: ________________________  Date: ___________

System ready for production: [ ] YES  [ ] NO

Next review date: ___________

---

## Incident Response Plan

### If Something Goes Wrong in Paper Trading
1. Kill switch immediately: `/api/kill-switch/activate`
2. Check logs: `docker-compose logs -f`
3. Identify issue
4. Fix configuration
5. Restart service: `docker-compose restart tradeos`
6. Resume: `/api/kill-switch/resume`
7. Resume paper trading

### If Something Goes Wrong in Live Trading
1. KILL SWITCH FIRST: `/api/kill-switch/activate`
2. Do NOT make emergency trades
3. Close all positions manually if needed (via exchange)
4. Assess damage
5. Check logs
6. Fix issue
7. Only resume if safe
8. Contact support if unsure

### If Database is Corrupted
1. Kill switch
2. Restore from backup: `bash scripts/restore.sh backups/tradeos_backup_YYYYMMDD_hhmmss.sql.gz`
3. Verify restore: `bash scripts/health-check.sh`
4. Resume

### If Signer Service is Compromised
1. Kill switch
2. Activate signer kill switch: `docker-compose exec signer touch SIGNER_KILLSWITCH`
3. Signer will refuse ALL signing requests (even from legitimate app)
4. Manual recovery needed

---

## Post-Deployment Maintenance

### Weekly
- [ ] `bash scripts/health-check.sh` passes
- [ ] No error spam in logs
- [ ] Backups created successfully
- [ ] Dashboard responsive

### Monthly
- [ ] Full system test (recreate small paper trade)
- [ ] Update all docker images: `docker-compose pull && docker-compose up -d`
- [ ] Review logs for patterns
- [ ] Rotate dashboard token
- [ ] Verify backups can be restored

### Quarterly
- [ ] Security audit of all configurations
- [ ] Review risk parameters vs. actual performance
- [ ] Test disaster recovery procedures
- [ ] Update this checklist

---

Good luck! 🚀

Remember: **A successful deployment is not the end, it's the beginning of operational responsibility.**
