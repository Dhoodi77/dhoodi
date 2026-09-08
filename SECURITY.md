# TradeOS Security Architecture

**For: Understanding the security boundaries and threat model**

---

## Executive Summary

TradeOS enforces a multi-layered security architecture designed so that no single compromise allows unauthorized trading.

```
┌─────────────────────────────────────────────────────────┐
│                  Web Dashboard                          │
│         (unauthenticated → 401 Unauthorized)            │
└───────────────┬─────────────────────────────────────────┘
                │ HTTPS + Dashboard Token
┌───────────────▼─────────────────────────────────────────┐
│              TradeOS Application                        │
│         (trading logic, risk engine, agents)            │
│    ❌ Does NOT hold private keys                        │
│    ❌ Does NOT sign transactions                        │
└───────────────┬─────────────────────────────────────────┘
                │ HTTP + Signer Token (internal only)
┌───────────────▼─────────────────────────────────────────┐
│          External Signer Service                        │
│      (independent key management)                       │
│    ✓ Holds private keys (protected)                     │
│    ✓ Signs transactions only                            │
│    ✓ Enforces independent policy                        │
│    ✓ Kill switch (filesystem-based)                     │
└───────────────┬─────────────────────────────────────────┘
                │ TLS (or localhost-only)
┌───────────────▼─────────────────────────────────────────┐
│            Blockchain Network                           │
│         (Solana, Ethereum, etc.)                        │
└─────────────────────────────────────────────────────────┘
```

The key principle: **All critical security decisions are deterministic (not subject to AI reasoning).**

---

## Security Layers

### Layer 1: Authentication & Authorization

**Dashboard Access:**
- Requires `TRADEOS_DASHBOARD_TOKEN` (randomly generated, 32 bytes)
- Token validated on every API request
- 401 Unauthorized if missing or wrong
- Rate limited to prevent brute force (5 tries per minute at auth endpoint)

**Database Access:**
- PostgreSQL connection requires password (configured via `DB_PASSWORD`)
- Separate user account (`tradeos`) with limited permissions
- All credentials stored in `.env` (not in code or git)

**Signer Access:**
- Requires `SIGNER_TOKEN` (independently generated, 32 bytes)
- Must match between TradeOS and signer service
- Only TradeOS app can send signing requests (not exposed to internet)

### Layer 2: Secrets Management

**Private Keys:**
- ❌ NEVER stored in TradeOS process memory
- ❌ NEVER logged anywhere
- ❌ NEVER transmitted over unencrypted connections
- ✓ Stored ONLY in external signer service
- ✓ Protected by filesystem permissions (`chmod 400`)
- ✓ Read-only by signer OS user
- ✓ Isolated in separate container/process

**API Keys:**
- Environment variables only (`ANTHROPIC_API_KEY`, `TRADEOS_HELIUS_API_KEY`, etc.)
- Never in logs (redacted by logging layer)
- Never in database
- Never in version control

**Dashboard Token:**
- Treated like a password
- Changed monthly (in production)
- Never shared via chat or email
- Never stored in browser LocalStorage in production (only in memory)

### Layer 3: Network Security

**HTTPS/TLS:**
- All connections encrypted in transit
- Certificate validation enforced
- Self-signed certs acceptable for development
- Let's Encrypt recommended for production

**Nginx Reverse Proxy:**
- Only HTTPS exposed to internet (port 443)
- HTTP redirects to HTTPS (port 80)
- Rate limiting: 10 requests/sec per IP (API), 5/min at auth endpoint
- Security headers:
  ```
  Strict-Transport-Security: HSTS enforced
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Content-Security-Policy: strict (no external scripts)
  Permissions-Policy: deny all sensors/camera/mic
  ```

**Internal Network:**
- Signer service runs on `127.0.0.1:8471` (localhost only)
- Not exposed to internet
- Only accessible from TradeOS app container
- No credentials needed if on same host

### Layer 4: Deterministic Risk Gating

**Code-Level Constraints** (cannot be overridden by AI or configuration):

```python
# Hard cap (product policy, not configurable)
HARD_INITIAL_TRADE_CAP_USD = 20.0

# Every trade checked against:
- maximum initial position: $20 (code-level ceiling)
- maximum daily loss: configured (default $40)
- maximum portfolio exposure: configured (default $100)
- maximum slippage: configured (default 3%)
- maximum gas fees: configured (default $2)
- maximum open positions: configured (default 5)
- maximum drawdown: configured (default 25%)
```

**Risk Engine (Independent from AI):**
1. Market data arrives
2. Discovery pipeline generates opportunities
3. Agents analyze and recommend
4. Risk engine checks DETERMINISTICALLY:
   - All 9 parameters above ✓
   - Kill switch status ✓
   - Market conditions ✓
   - Wallet authorization ✓
5. Only then: execute or reject

**The Risk Engine REFUSES to trade if:**
- Any risk parameter missing (explicitly required to trade)
- Kill switch is active
- Signer returns error
- Transaction validation fails
- Market data stale (> 1 minute)

### Layer 5: Transaction Signing

**Signer Policy (Independent):**
The signer service enforces its own policy regardless of what TradeOS asks:

1. **Solana:**
   - Only signs transactions where fee payer = signer's own key
   - Rolling hourly rate limit (default 30 signing requests/hour)
   - Refuses signing if `SIGNER_KILLSWITCH` file exists

2. **EVM:**
   - Maximum transaction value cap (default 0.1 ETH)
   - Chain ID allowlist (only allowed chains)
   - Optional destination allowlist (contract whitelist)
   - Same rate limiting
   - Same kill switch

**What the signer CANNOT do:**
- ❌ Sign transactions for other addresses
- ❌ Sign transfers from wrong wallet
- ❌ Bypass its own policy
- ❌ Be manipulated by TradeOS (separate service)

### Layer 6: Kill Switch

**Two Independent Kill Switches:**

1. **API Kill Switch (TradeOS level):**
   ```bash
   POST /api/kill-switch/activate
   ```
   - Stops new trade requests immediately
   - Rejects signing requests
   - Preserves all audit records
   - Can be resumed: POST /api/kill-switch/resume

2. **Filesystem Kill Switch (Signer level):**
   ```bash
   touch signer/SIGNER_KILLSWITCH
   ```
   - Immediately stops signer from signing anything
   - Independent of TradeOS
   - Does not require restarting services
   - Works even if TradeOS or API is compromised

**Both must be reset before trading resumes.**

### Layer 7: Audit & Logging

**Immutable Audit Trail:**
- Every trade recorded with timestamp and actor
- Every agent decision logged with reasoning
- Every risk check recorded (accept/reject)
- Every kill switch activation logged
- Database-backed (not easy to tamper with)

**Redacted Logging:**
- Private keys never logged
- API keys never logged
- Wallet addresses masked in some contexts
- All logs searchable for debugging

**Log Access:**
```bash
docker-compose logs -f tradeos    # Real-time app logs
docker-compose logs -f signer     # Signer logs
docker-compose logs -f postgres   # Database logs
```

### Layer 8: Container Isolation

**Docker Namespacing:**
- Each service in separate container
- Separate Linux users (tradeos, signer, postgres)
- Signer cannot read TradeOS code or database
- TradeOS cannot access signer filesystem
- Limited to configured volumes only

**Resource Limits:**
- Memory: max 2GB per container
- CPU: shared across all
- Disk: persistent volumes only
- Network: internal bridge network

### Layer 9: Database Access Control

**SQLite (Development):**
- File-based, permissions restricted
- WAL mode enabled
- All writes are explicit transactions

**PostgreSQL (Production):**
- `tradeos` user with limited schema permissions
- No superuser access
- Connection pooling (1-5 connections)
- Automatic credential rotation possible

**Sensitive Fields:**
- `api_keys` table: not accessible to agents
- `kv_state` table: limited to system-level access
- `password_fields`: never stored (only tokens)

---

## Threat Model

### Threat: AI Agent Exploited or Misbehaves

**Vector:** LLM injection, prompt exploitation, or model fine-tuning attack

**Defense:**
- AI never receives private keys ✓
- AI never signs transactions ✓
- AI recommendations checked by deterministic risk engine ✓
- Risk engine independent of AI ✓
- Kill switch stops trading immediately ✓

**Result:** Agent cannot trade beyond risk limits even if compromised

### Threat: TradeOS Process Compromised

**Vector:** Buffer overflow, code injection, supply chain attack

**Defense:**
- TradeOS runs as non-root user (`tradeos`) ✓
- No shell access (containers)
- No private keys to steal ✓
- Signer service independent ✓
- Signer can refuse signing requests ✓
- Kill switch disables trading ✓

**Result:** Cannot sign transactions without signer cooperation

### Threat: Database Stolen/Corrupted

**Vector:** Backup theft, ransomware, SQL injection

**Defense:**
- No plaintext secrets in database ✓
- Private keys not in database ✓
- API keys not in database ✓
- Audit trail immutable (signed checksums optional) ✓
- Backups encrypted in transit ✓
- Backups stored off-site ✓

**Result:** Attacker gets trading history, not keys

### Threat: Signer Service Compromised

**Vector:** Code exploit, SSH breach, container escape

**Defense:**
- Separate OS user (not tradeos) ✓
- Separate container with own filesystem ✓
- Filesystem kill switch (`touch SIGNER_KILLSWITCH`) ✓
- Independent policy enforced ✓
- Rate limiting (30 requests/hour) ✓
- Can be run on separate machine ✓

**Result:** Kill switch prevents all signing immediately

### Threat: API Keys Leaked (Anthropic, Helius, etc.)

**Vector:** Env var exposure, log leakage, git history

**Defense:**
- Env vars only (not in code) ✓
- Redacted from logs ✓
- .env in .gitignore ✓
- Separate tokens per environment ✓
- Rotation possible without deployment ✓

**Result:** Limited damage (read-only for discovery APIs)

### Threat: Network Man-in-the-Middle

**Vector:** ISP eavesdropping, Wifi interception

**Defense:**
- HTTPS/TLS for all external connections ✓
- Certificate validation required ✓
- Internal network (Docker bridge, localhost-only) ✓
- Signer over localhost or TLS ✓

**Result:** Cannot intercept or modify transactions

### Threat: Accidental Misconfiguration

**Vector:** Wrong risk limits, disabled kill switch, missing token

**Defense:**
- All risk parameters required (explicit configuration) ✓
- Startup validation (refuses to trade if incomplete) ✓
- Dashboard shows all active limits ✓
- Configuration in .env (easy to review) ✓
- Deployment script validates all prerequisites ✓

**Result:** System refuses to trade until properly configured

---

## Best Practices for Operators

### Before Going Live

1. **Secure the VPS:**
   ```bash
   sudo apt-get update && sudo apt-get upgrade -y
   sudo ufw default deny incoming
   sudo ufw allow 22/tcp   # SSH
   sudo ufw allow 80/tcp   # HTTP
   sudo ufw allow 443/tcp  # HTTPS
   sudo ufw enable
   ```

2. **Disable Root Login:**
   ```bash
   sudo sed -i 's/PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
   sudo systemctl restart sshd
   ```

3. **Generate Strong Tokens:**
   - Never reuse tokens
   - Never use predictable values
   - Rotate monthly

4. **Protect .env File:**
   ```bash
   chmod 600 .env
   ls -la .env  # Should show: -rw------- 
   ```

5. **Backup Encryption:**
   ```bash
   gpg --symmetric backups/tradeos_backup_*.sql.gz
   ```

6. **Monitor Access:**
   ```bash
   tail -f /var/log/auth.log  # SSH logins
   tail -f /var/log/syslog    # System events
   docker-compose logs -f     # Application logs
   ```

### During Operation

1. **Monthly Token Rotation:**
   ```bash
   TRADEOS_DASHBOARD_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
   # Update .env and restart
   docker-compose restart tradeos
   ```

2. **Audit Log Review:**
   ```bash
   docker-compose exec postgres psql -U tradeos -d tradeos -c "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 50;"
   ```

3. **Security Update Monitoring:**
   ```bash
   apt-get update
   apt-get upgrade --security-only -y
   docker-compose pull  # Get latest base images
   ```

4. **Kill Switch Testing (Monthly):**
   - Test API kill switch
   - Test filesystem kill switch
   - Verify resume works
   - Verify trades blocked while active

### Incident Response

**If Private Key Exposed:**
1. Activate kill switch immediately
2. Create new wallet with new key
3. Transfer remaining funds
4. Investigate how it leaked
5. Update security procedures

**If Dashboard Token Leaked:**
1. Generate new token: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`
2. Update .env
3. Restart: `docker-compose restart tradeos`
4. Review audit logs for unauthorized access

**If Database Corrupted:**
1. Activate kill switch
2. Restore from backup: `bash scripts/restore.sh backups/...`
3. Verify health: `bash scripts/health-check.sh`
4. Resume

---

## Security Checklist

Before trading with real capital:

- [ ] VPS hardened (firewall, SSH key only, updates)
- [ ] .env permissions set to 600
- [ ] .env NOT in git history
- [ ] Tokens generated securely (not reused)
- [ ] HTTPS working with valid certificate
- [ ] Dashboard token tested
- [ ] Kill switch tested (both API and filesystem)
- [ ] Risk limits reviewed and appropriate
- [ ] Paper trading successful
- [ ] Signer kill switch understood and tested
- [ ] Backup and restore procedures tested
- [ ] Off-site backup location configured
- [ ] Monitoring and alerting configured
- [ ] Incident response plan documented
- [ ] Team trained on security procedures

---

## Compliance & Auditability

**What TradeOS Records:**
- Every trade decision (entry, exit, risk check)
- Every agent's reasoning and confidence
- Every risk gate decision (accept/reject with reason)
- Every kill switch activation
- Every signer refusal
- Timestamps for all events
- Original market data at decision time

**What You Can Audit:**
```bash
# All trades
SELECT * FROM trades WHERE created_at >= '2024-01-01' ORDER BY created_at;

# All agent decisions
SELECT * FROM agent_decisions ORDER BY created_at DESC LIMIT 100;

# All risk events (rejections)
SELECT * FROM risk_events ORDER BY created_at DESC;

# Kill switch history
SELECT * FROM system_events WHERE kind LIKE '%kill%' ORDER BY created_at DESC;

# Post-trade analysis
SELECT * FROM post_trade_reviews ORDER BY created_at DESC;
```

**Export for Tax/Compliance:**
Logs can be exported and analyzed for:
- Position P&L calculation
- Tax lot identification
- Wash sale detection
- Trading pattern analysis
- Risk compliance verification

---

## References

- NIST Cybersecurity Framework: https://www.nist.gov/cyberframework
- OWASP Top 10: https://owasp.org/www-project-top-ten/
- CWE/SANS Top 25: https://cwe.mitre.org/top25/

---

**Remember:** Security is not a feature to be added later. It's built in from the start. Never trust a trading system that keeps your private keys in its database or logs.

Good luck! 🔐

