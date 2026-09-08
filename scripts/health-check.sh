#!/bin/bash
# TradeOS Health Check Script
# Monitors system health and alerts on issues

set -euo pipefail

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
HEALTH_FILE="data/health.log"

check_service() {
    local name=$1
    local url=$2
    local expected_code=${3:-200}

    if curl -sf -o /dev/null -w "%{http_code}" "$url" | grep -q "$expected_code"; then
        echo "✓ $name"
        return 0
    else
        echo "❌ $name"
        return 1
    fi
}

check_docker() {
    local service=$1

    if docker-compose ps | grep -q "Up.*$service"; then
        echo "✓ Docker: $service"
        return 0
    else
        echo "❌ Docker: $service"
        return 1
    fi
}

main() {
    echo ""
    echo "🏥 TradeOS Health Check - $TIMESTAMP"
    echo "====================================="
    echo ""

    local all_healthy=1

    # Docker services
    echo "Services:"
    check_docker "tradeos-app" || all_healthy=0
    check_docker "tradeos-postgres" || all_healthy=0
    check_docker "tradeos-signer" || all_healthy=0
    check_docker "tradeos-redis" || all_healthy=0
    check_docker "tradeos-nginx" || all_healthy=0

    echo ""
    echo "API Endpoints:"
    check_service "Health endpoint" "http://localhost:8420/health" || all_healthy=0
    check_service "Desk endpoint" "http://localhost:8420/api/desk" "401" || true  # Expects 401 without auth

    echo ""
    echo "Database:"
    if docker-compose exec -T postgres pg_isready -U tradeos -d tradeos >/dev/null 2>&1; then
        echo "✓ PostgreSQL"
    else
        echo "❌ PostgreSQL"
        all_healthy=0
    fi

    echo ""
    echo "Disk Space:"
    usage=$(df / | awk 'NR==2 {print int($5)}')
    echo "  Root: $usage% used"
    if [ "$usage" -gt 80 ]; then
        echo "  ⚠️  Disk usage above 80%"
        all_healthy=0
    fi

    echo ""
    echo "Memory Usage:"
    docker stats --no-stream --format "table {{.Container}}\t{{.MemUsage}}" | grep tradeos || true

    echo ""
    if [ $all_healthy -eq 1 ]; then
        echo "✅ All systems operational"
    else
        echo "⚠️  Some services may need attention"
    fi

    echo ""
    echo "Recent logs:"
    echo "  $(docker-compose logs tradeos --tail=1 2>/dev/null | tail -1 || echo 'No logs')"
}

main "$@"
