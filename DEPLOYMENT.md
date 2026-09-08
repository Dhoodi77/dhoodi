# TradeOS Production Deployment Guide

**For: Non-DevOps users deploying a 24/7 autonomous trading system**

---

## ⚠️ CRITICAL SAFETY INFORMATION

This guide helps you deploy TradeOS to run 24/7 on a Linux server. Before you begin, understand:

1. **Live trading is DISABLED by default.** You must explicitly enable it after testing paper trading.
2. **The system enforces hard limits:** maximum $20 per trade, maximum $3 planned loss, enforced by deterministic code.
3. **Private keys are never stored in TradeOS.** They live in a separate "signer" service that can refuse signing requests independently.
4. **A kill switch exists outside the AI logic.** Even if the AI malfunctions, you can stop trading immediately.
5. **All settings are in `.env` configuration files.** Sensitive data is never in code.

---

## Overview: What Gets Deployed

This deployment creates a complete production system:

```
┌─────────────────────────────────────────────┐
│         Your VPS Running Linux              │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │  Nginx (HTTPS Reverse Proxy)         │  │
│  │  - SSL/TLS encryption                │  │
│  │  - Rate limiting                     │  │
│  │  - Security headers                  │  │
│  └──────────────────────────────────────┘  │
│         ↓                                    │
│  ┌──────────────────────────────────────┐  │
│  │  TradeOS Application                 │  │
│  │  - AI agents                         │  │
│  │  - Trading pipeline                  │  │
│  │  - Risk management                   │  │
│  │  - Web dashboard                     │  │
│  └──────────────────────────────────────┘  │
│    ↓                  ↓                     │
│  ┌──────────────┐   ┌─────────────────┐   │
│  │ PostgreSQL   │   │ Signer Service  │   │
│  │ Database     │   │ (Sign txns only)│   │
│  │ (persistent) │   │ (isolated keys) │   │
│  └──────────────┘   └─────────────────┘   │
│         ↓                                    │
│  ┌──────────────────────────────────────┐  │
│  │  Redis (job queue)                   │  │
│  │  - Future: async tasks               │  │
│  └──────────────────────────────────────┘  │
│                                             │
└─────────────────────────────────────────────┘
         ↓                        ↓
    Blockchain RPC        External APIs
    (Solana, EVM)        (Helius, Etherscan)
```

All services run in Docker containers. They restart automatically if they crash. Logs are available for debugging.

---

## YOU DO THIS

These steps require your personal action on the VPS.

### Step 1: Rent a VPS

**Where:** Any Linux hosting provider (AWS, DigitalOcean, Linode, Hetzner, etc.)

**Requirements:**
- Ubuntu 22.04 LTS or newer (or similar Debian-based distribution)
- At least 2 CPU cores
- At least 4GB RAM (8GB recommended)
- At least 50GB SSD storage
- Static public IP address
- Ability to run Docker

**Cost:** $10-30/month depending on provider

**After you rent the VPS:**
1. Log in via SSH: `ssh root@<your_ip_address>`
2. Update the system:
   ```bash
   apt-get update && apt-get upgrade -y
   ```

### Step 2: Install Docker and Docker Compose

Run these commands on your VPS:

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add your user to docker group (so you don't need sudo)
sudo usermod -aG docker $USER
newgrp docker

# Verify installation
docker --version
docker-compose --version
```

### Step 3: Clone TradeOS Repository

```bash
# Clone the repository
git clone https://github.com/dhoodi77/dhoodi.git tradeos
cd tradeos

# Switch to production branch (if using separate branch)
git checkout claude/crypto-ai-trading-system-in6tyq
```

### Step 4: Configure Your Domain Name

**You need a domain name** (e.g., `example.com`) to set up HTTPS.

Where to get one:
- Namecheap, GoDaddy, Route53, Cloudflare (usually $10-15/year)

**What you need to do:**
1. Buy a domain name pointing to your VPS's IP address
2. Point DNS `app.example.com` → your VPS IP address (or use `*.example.com` wildcard)
3. Wait 5-30 minutes for DNS to propagate

To check if DNS is working:
```bash
nslookup app.example.com
# Should return your VPS's IP address
```

### Step 5: Generate Required Tokens

On your VPS, run these commands to generate secure random tokens:

```bash
# Dashboard authentication token
python3 -c "import secrets;print(secrets.token_urlsafe(32))"
# Copy the output - you'll need it for .env

# Signer service token (must be same in both configs)
python3 -c "import secrets;print(secrets.token_urlsafe(32))"
# Copy the output - you'll need it for .env

# Database password
python3 -c "import secrets;print(secrets.token_urlsafe(16))"
# Copy the output - you'll need it for .env
```

### Step 6: Create .env Configuration File

On your VPS, in the `tradeos` directory:

```bash
# Copy template to .env
cp .env.production.template .env

# Edit with your generated tokens and settings
nano .env
```

**ABSOLUTELY REQUIRED fields** (search for `[REQUIRED]`):

| Field | What to enter | Example |
|-------|--------------|---------|
| `TRADEOS_DASHBOARD_TOKEN` | The first token you generated above | `AbCdEfGhIjKlMnOpQrStUvWxYz123456` |
| `DB_PASSWORD` | The database password you generated | `X9y8z7w6v5u4t3s2r1q0p...` |
| `SIGNER_TOKEN` | The signer token you generated | `AbCdEfGhIjKlMnOpQrStUvWxYz654321` |
| `DOMAIN_NAME` | Your domain (from Step 4) | `example.com` |

**Leave these empty** (they are optional):
- `ANTHROPIC_API_KEY` - Add later if you want AI agents (recommended)
- `TRADEOS_HELIUS_API_KEY` - Add later for Solana scanning (optional)
- `TRADEOS_ETHERSCAN_API_KEY` - Add later for EVM scanning (optional)
- All the RPC endpoints - Keep empty for paper trading

**ABSOLUTELY leave these UNCHANGED** (hardcoded safety limits):
```
TRADEOS_RISK_MAX_INITIAL_POSITION_USD=20
TRADEOS_RISK_MAX_POSITION_USD=20
TRADEOS_RISK_MAX_DAILY_LOSS_USD=40
TRADEOS_RISK_MAX_PORTFOLIO_EXPOSURE_USD=100
```

Save with `Ctrl+X`, then `Y`, then `Enter`.

### Step 7: Verify .env is Secure

```bash
# Make .env readable only by you
chmod 600 .env

# Verify no sensitive data is in git
cat .gitignore | grep env
# Should show: .env
```

### Step 8: Configure SSL Certificates

For **development/testing only**, self-signed certificates are fine:

```bash
# Already created by deploy script
ls -la ssl/
```

For **production with real HTTPS**, get a Let's Encrypt certificate:

```bash
# Install certbot
apt-get install -y certbot python3-certbot-nginx

# Get certificate (replace with your domain)
sudo certbot certonly --standalone -d app.example.com -d example.com

# Copy to ssl directory
sudo cp /etc/letsencrypt/live/app.example.com/fullchain.pem ssl/tradeos.crt
sudo cp /etc/letsencrypt/live/app.example.com/privkey.pem ssl/tradeos.key
sudo chmod 644 ssl/tradeos.crt
sudo chmod 600 ssl/tradeos.key

# Auto-renewal (certbot handles this automatically)
```

### Step 9: Create Signer Directory (if using live trading)

If you plan to eventually enable live trading, prepare the signer:

```bash
# Create directory for keys (they go here, NOT in code)
mkdir -p signer_keys
chmod 700 signer_keys

# When you're ready for live trading:
# Place your keys here:
# - id.json (Solana keypair from: solana-cli config get identity)
# - evm.key (EVM private key in hex format, 0x-prefixed)

# NEVER commit this directory
echo "signer_keys/" >> .gitignore
```

---

## CLAUDE DOES THIS

These are fully automated. Just run the deployment script.

### Automated: Full Deployment

On your VPS, in the `tradeos` directory:

```bash
# Run the deployment script
bash scripts/deploy.sh
```

**What this script does:**
1. ✓ Checks that Docker is installed
2. ✓ Verifies all required fields in `.env`
3. ✓ Creates SSL certificates (self-signed for now)
4. ✓ Builds Docker images
5. ✓ Starts PostgreSQL database
6. ✓ Starts Redis cache
7. ✓ Starts the signer service
8. ✓ Starts the main TradeOS app
9. ✓ Starts Nginx reverse proxy
10. ✓ Verifies all services are healthy

**Expected output:**
```
🚀 TradeOS Production Deployment
================================
✓ Checking prerequisites...
✓ Creating necessary directories...
✓ Setting up environment...
✓ Setting up SSL certificates...
✓ Setting up signer configuration...
✓ Building Docker images...
✓ Initializing database...
✓ Starting all services...
✓ Services started

✅ TradeOS Production Deployment Complete!

Access points:
  Web Dashboard: https://localhost/
  API Health: https://localhost/health

Useful commands:
  docker-compose logs -f              # View logs
  docker-compose ps                   # See all services
  ./scripts/backup.sh                 # Backup database
  ./scripts/health-check.sh           # Check system health
```

---

## Testing After Deployment

### Test 1: Dashboard Access

```bash
# Check if service is running
curl -I https://localhost/health

# Expected response: 200 OK
```

### Test 2: Paper Trading

1. Open dashboard: `https://your-domain/` or `https://localhost/`
2. Authenticate with your `TRADEOS_DASHBOARD_TOKEN`
3. Verify you see:
   - Starting balance: $500 (or configured amount)
   - All agents listed (Xavi, Messi, Ronny, etc.)
   - 0 open positions
   - 0 trades
4. The monitor should be running and refreshing every 12 seconds

### Test 3: Database Backup

```bash
# Create a backup
bash scripts/backup.sh

# Verify backup was created
ls -lh backups/
```

### Test 4: Health Check

```bash
# Run health check
bash scripts/health-check.sh

# Should show all services as ✓
```

---

## Continuous Operation

### Monitoring (Check Weekly)

```bash
# View current status
docker-compose ps

# Check logs for errors
docker-compose logs --tail=100 tradeos

# Full health check
bash scripts/health-check.sh

# See resource usage
docker stats
```

### Backups (Automatic)

Set up automatic daily backups using a cron job:

```bash
# Edit crontab
crontab -e

# Add this line (runs daily at 2 AM):
0 2 * * * cd /home/user/tradeos && bash scripts/backup.sh >> /var/log/tradeos-backup.log 2>&1
```

### Restart Services (if needed)

```bash
# Restart just the app
docker-compose restart tradeos

# Restart everything
docker-compose restart

# Check that it came back up
bash scripts/health-check.sh
```

### View Logs

```bash
# Last 100 lines
docker-compose logs --tail=100

# Follow in real-time (Ctrl+C to stop)
docker-compose logs -f

# Just the app
docker-compose logs tradeos
```

---

## Enabling Anthropic API (Optional but Recommended)

The AI agents work without the API (using heuristics), but they're much better with Claude:

1. Get API key from: https://console.anthropic.com
2. Edit `.env`:
   ```bash
   nano .env
   ```
3. Set:
   ```
   ANTHROPIC_API_KEY=sk-ant-...your-key-here...
   ```
4. Restart app:
   ```bash
   docker-compose restart tradeos
   ```

---

## Enabling Live Trading (DANGEROUS - ONLY AFTER PAPER TESTING)

**DO NOT enable live trading until:**
1. ✓ Paper trading works for at least 24 hours
2. ✓ You've verified the $20 limit is enforced
3. ✓ You've verified the $3 loss limit is enforced
4. ✓ You've tested the kill switch works
5. ✓ You've reviewed all risk parameters
6. ✓ You understand you could lose all capital

**To enable live trading:**

### Step 1: Set Up Wallet and Signer

1. Create a dedicated trading wallet with very limited funds
2. Fund it with only what you're willing to lose (e.g., $100 for testing)
3. Get the wallet's keypair or private key
4. Place in `signer_keys/`:
   - Solana: `id.json`
   - EVM: `evm.key`

### Step 2: Configure RPC Endpoints

Edit `.env` and add RPC endpoints for chains you want to trade:

```bash
nano .env
```

Add (replace with real RPC URLs):
```
TRADEOS_RPC_SOLANA=https://api.mainnet-beta.solana.com
TRADEOS_RPC_ETHEREUM=https://eth-mainnet.alchemyapi.io/v2/YOUR-KEY
TRADEOS_RPC_BASE=https://base-mainnet.g.alchemy.com/v2/YOUR-KEY
# ... etc for other chains
```

### Step 3: Set Confirmation Phrase

Edit `.env`:
```bash
nano .env
```

Change:
```
TRADEOS_LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS
```

### Step 4: Change Mode

Edit `.env`:
```bash
nano .env
```

Change:
```
TRADEOS_MODE=live
```

### Step 5: Restart

```bash
docker-compose restart tradeos
```

### Step 6: Verify Readiness

```bash
# Check if live trading is actually enabled
curl -H "Authorization: Bearer $TRADEOS_DASHBOARD_TOKEN" \
  http://localhost:8420/api/live/readiness

# Should show: "ready": true (if all prerequisites met)
# Or list missing prerequisites
```

---

## Emergency Procedures

### Kill Switch: Stop All Trading

```bash
# API-based kill switch
curl -X POST \
  -H "Authorization: Bearer $TRADEOS_DASHBOARD_TOKEN" \
  http://localhost:8420/api/kill-switch/activate \
  -d '{"reason": "manual emergency stop"}'

# Or use the dashboard UI:
# 1. Open https://your-domain/
# 2. Find "🛑 KILL SWITCH" button
# 3. Click it
```

**What this does:**
- Stops accepting new trade requests
- Stops signing requests
- Preserves all audit records
- Allows manual resume when safe

### Signer Kill Switch (Independent)

If the signer service is compromised or malfunctioning:

```bash
# Connect to signer container
docker-compose exec signer touch SIGNER_KILLSWITCH

# The signer immediately refuses ALL signing requests
# Check logs:
docker-compose logs signer
```

### Database Recovery

If the database is corrupted:

```bash
# List available backups
ls -lh backups/

# Restore from backup
bash scripts/restore.sh backups/tradeos_backup_20240908_120000.sql.gz

# Verify
bash scripts/health-check.sh
```

### Full Reset (Last Resort)

If something is seriously broken:

```bash
# ⚠️  This deletes all data
docker-compose down -v
rm -rf data/ backups/
bash scripts/deploy.sh
```

---

## Troubleshooting

### Services won't start

```bash
# Check logs
docker-compose logs

# Common issues:
# - Port already in use: check netstat -tulpn
# - Disk full: check df -h
# - Memory full: check free -h
```

### Dashboard shows 401 Unauthorized

```bash
# Wrong token
# Check that your TRADEOS_DASHBOARD_TOKEN matches what you set in .env
# Regenerate if needed: python3 -c "import secrets;print(secrets.token_urlsafe(32))"
```

### Database connection error

```bash
# PostgreSQL not ready
docker-compose logs postgres

# Usually just needs more time to start (first startup can take 30 seconds)
# Wait and try again:
docker-compose restart tradeos
```

### High memory usage

```bash
# Check what's using memory
docker stats

# Usual culprits:
# - Redis cache: normal, can restart if needed
# - PostgreSQL: increase work_mem in docker-compose.yml
# - Python app: may be memory leak, check logs
```

---

## Verification Checklist

Use this to confirm everything is working:

- [ ] Dashboard accessible at https://your-domain/
- [ ] HTTPS is working (lock icon in browser)
- [ ] Authentication required (401 without token, 200 with token)
- [ ] Database running: `docker-compose exec postgres psql -U tradeos -d tradeos -c "SELECT 1;"`
- [ ] Signer running: `docker-compose logs signer | grep -i ready`
- [ ] Paper mode configured: `grep TRADEOS_MODE .env`
- [ ] Risk limits set: `grep TRADEOS_RISK .env | grep -v "^#"`
- [ ] Backups working: `bash scripts/backup.sh` creates a file
- [ ] Health check passes: `bash scripts/health-check.sh` shows all ✓
- [ ] Kill switch works: `/api/kill-switch/activate` endpoint responds
- [ ] **Live mode DISABLED**: `grep "TRADEOS_MODE=live" .env` shows nothing (or shows "=paper")

---

## Security Best Practices

1. **Never commit `.env` to git** (already in `.gitignore`)
2. **Never share `TRADEOS_DASHBOARD_TOKEN`** (like a password)
3. **Never paste `.env` in chat or logs**
4. **Update system regularly:**
   ```bash
   apt-get update && apt-get upgrade -y
   ```
5. **Use strong SSH keys** (no password auth)
6. **Lock down firewall** (only 80, 443 from outside):
   ```bash
   ufw allow 22/tcp  # SSH
   ufw allow 80/tcp  # HTTP
   ufw allow 443/tcp # HTTPS
   ufw default deny incoming
   ufw enable
   ```
7. **Rotate dashboard token monthly** (regenerate and restart)
8. **Keep backups off-site** (copy to secure cloud storage)

---

## Support and Debugging

### Getting Help

1. Check logs: `docker-compose logs -f`
2. Run health check: `bash scripts/health-check.sh`
3. Test connectivity: `curl http://localhost:8420/health`

### Common Questions

**Q: How do I change risk limits?**
A: Edit `.env`, update the `TRADEOS_RISK_*` values, restart: `docker-compose restart tradeos`

**Q: How do I add more funds to paper trading?**
A: Edit `.env`, change `TRADEOS_PAPER_STARTING_BALANCE_USD`, run: `docker-compose exec tradeos python3 -m tradeos.accounting reset_paper`

**Q: Can I run multiple instances?**
A: No, currently designed for single instance per database. Use separate servers for different trading strategies.

**Q: How do I disable live trading without redeploying?**
A: Edit `.env`, set `TRADEOS_LIVE_TRADING_CONFIRM=` (empty), restart app.

---

## Summary

You now have:

✅ **A secure, production-grade trading system**
- Runs 24/7 with automatic restarts
- All data persisted to PostgreSQL
- Backup system in place
- HTTPS encryption
- Token-based authentication

✅ **Safety boundaries enforced**
- $20 maximum per trade (code-level, not configurable)
- $3 maximum planned loss
- Kill switches (both API and signer-level)
- Risk parameters checked before every trade

✅ **Monitoring and debugging**
- Health checks every 30 seconds
- Logs available for inspection
- Automatic backup retention

✅ **Paper trading enabled by default**
- Test everything without real money
- Full system operational
- Ready to go live when you're confident

**Next steps:**
1. Test paper trading for 24+ hours
2. Review the kill switch and understand how to use it
3. Only then consider enabling live trading
4. Start with very small amounts ($50-100)
5. Monitor closely for the first week

Good luck! 🚀

