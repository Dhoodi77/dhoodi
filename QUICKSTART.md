# TradeOS Quick Start (Local Development)

Get TradeOS running on your machine in 5 minutes.

## Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Basic command line knowledge

## 1. Clone & Navigate

```bash
git clone https://github.com/dhoodi77/dhoodi.git
cd dhoodi
git checkout claude/crypto-ai-trading-system-in6tyq
```

## 2. Generate Tokens

```bash
# Dashboard token
python3 -c "import secrets;print(secrets.token_urlsafe(32))"

# Signer token (use same value for both)
python3 -c "import secrets;print(secrets.token_urlsafe(32))"

# Database password
python3 -c "import secrets;print(secrets.token_urlsafe(16))"
```

## 3. Create .env

```bash
cp .env.production.template .env
nano .env
```

Fill in:
```
TRADEOS_DASHBOARD_TOKEN=<your_dashboard_token>
DB_PASSWORD=<your_db_password>
SIGNER_TOKEN=<your_signer_token>
DOMAIN_NAME=localhost
TRADEOS_MODE=paper
```

## 4. Start Everything

```bash
bash scripts/deploy.sh
```

## 5. Access Dashboard

```
https://localhost/ (ignore SSL warning for self-signed cert)
```

Authenticate with your `TRADEOS_DASHBOARD_TOKEN`.

## Useful Commands

```bash
# View logs (real-time)
docker-compose logs -f

# Check service status
docker-compose ps

# Run health check
bash scripts/health-check.sh

# Backup database
bash scripts/backup.sh

# Access database CLI
docker-compose exec postgres psql -U tradeos -d tradeos

# Stop everything
docker-compose down

# Delete everything (fresh start)
docker-compose down -v && rm -rf data/
```

## What You'll See

1. **Dashboard:** Portfolio status, agents, opportunities
2. **Portfolio:** Starts with $500 paper money
3. **Agents:** Xavi (control), Messi (research), etc.
4. **Opportunities:** Discovery and analysis pipeline
5. **Positions:** Open and closed trades

## Test Paper Trading

1. Keep dashboard open
2. Monitor should run every 30 seconds
3. Should discover tokens
4. Should analyze them
5. Should create positions if criteria met
6. Positions should close on exit rules

## Next: Production Deployment

See `DEPLOYMENT.md` for setting up on a VPS with HTTPS.

