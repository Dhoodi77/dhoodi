#!/bin/bash
# TradeOS Database Backup Script
# Backs up PostgreSQL database and trading state

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-.}/backups"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/tradeos_backup_${TIMESTAMP}.sql.gz"

echo "🔄 TradeOS Database Backup"
echo "=========================="

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Perform backup
echo "Backing up database..."
if docker-compose exec -T postgres pg_dump -U tradeos tradeos | gzip > "$BACKUP_FILE"; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "✓ Backup successful: $BACKUP_FILE ($SIZE)"
else
    echo "❌ Backup failed"
    exit 1
fi

# Cleanup old backups
echo "Cleaning up backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name "tradeos_backup_*.sql.gz" -type f -mtime +${RETENTION_DAYS} -delete

# List recent backups
echo ""
echo "Recent backups:"
ls -lh "$BACKUP_DIR"/tradeos_backup_*.sql.gz 2>/dev/null | tail -5 || echo "  (no backups found)"

echo ""
echo "✅ Backup complete"
