#!/bin/bash
# TradeOS Database Restore Script
# Restores PostgreSQL database from backup

set -euo pipefail

BACKUP_FILE="${1:-}"

if [ -z "$BACKUP_FILE" ]; then
    echo "Usage: $0 <backup_file>"
    echo ""
    echo "Available backups:"
    ls -lh backups/tradeos_backup_*.sql.gz 2>/dev/null || echo "  (no backups found)"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ Backup file not found: $BACKUP_FILE"
    exit 1
fi

echo "⚠️  WARNING: This will overwrite the current database!"
echo "Backup file: $BACKUP_FILE"
echo ""
read -p "Continue with restore? (yes/no): " -r response

if [ "$response" != "yes" ]; then
    echo "Restore cancelled"
    exit 0
fi

echo ""
echo "🔄 Restoring database..."

# Stop the app (but keep PostgreSQL running)
docker-compose stop tradeos

# Drop existing database
echo "  Dropping existing database..."
docker-compose exec -T postgres dropdb -U tradeos tradeos || true

# Recreate database
echo "  Creating new database..."
docker-compose exec -T postgres createdb -U tradeos tradeos

# Restore from backup
echo "  Restoring from backup..."
if zcat "$BACKUP_FILE" | docker-compose exec -T postgres psql -U tradeos tradeos; then
    echo "✓ Database restored successfully"
else
    echo "❌ Restore failed"
    exit 1
fi

# Restart app
echo "  Restarting application..."
docker-compose start tradeos

sleep 5

if curl -sf http://localhost:8420/health >/dev/null 2>&1; then
    echo "✓ Application restarted successfully"
else
    echo "⚠️  Application may still be starting. Check logs with: docker-compose logs -f"
fi

echo ""
echo "✅ Restore complete"
