#!/bin/bash
# Production deployment script for TradeOS
# This script sets up a complete production environment on a Linux VPS

set -euo pipefail

echo "🚀 TradeOS Production Deployment"
echo "================================"

# Check prerequisites
check_prerequisites() {
    echo "✓ Checking prerequisites..."

    command -v docker >/dev/null 2>&1 || { echo "❌ Docker is required but not installed"; exit 1; }
    command -v docker-compose >/dev/null 2>&1 || { echo "❌ Docker Compose is required but not installed"; exit 1; }

    if ! groups | grep -q docker; then
        echo "⚠️  Add your user to docker group: sudo usermod -aG docker \$USER"
        exit 1
    fi

    echo "✓ All prerequisites met"
}

# Generate .env if it doesn't exist
setup_environment() {
    echo "✓ Setting up environment..."

    if [ ! -f .env ]; then
        cp .env.production.template .env
        echo "⚠️  Created .env from template. EDIT IT NOW:"
        echo "   nano .env"
        echo ""
        echo "Required fields to fill:"
        echo "  - TRADEOS_DASHBOARD_TOKEN"
        echo "  - DB_PASSWORD"
        echo "  - SIGNER_TOKEN"
        echo "  - DOMAIN_NAME (for HTTPS)"
        echo ""
        exit 1
    fi

    # Verify required fields
    required_fields=("TRADEOS_DASHBOARD_TOKEN" "DB_PASSWORD" "SIGNER_TOKEN" "DOMAIN_NAME")
    for field in "${required_fields[@]}"; do
        if ! grep -q "^$field=\[REQUIRED\]" .env && ! grep -q "^$field=\[" .env; then
            value=$(grep "^$field=" .env | cut -d'=' -f2-)
            if [ -z "$value" ] || [ "$value" = "[REQUIRED]" ] || [ "$value" = "[OPTIONAL]" ]; then
                echo "❌ Missing required field: $field"
                exit 1
            fi
        fi
    done

    chmod 600 .env
    echo "✓ Environment configured"
}

# Create SSL certificates
setup_ssl() {
    echo "✓ Setting up SSL certificates..."

    mkdir -p ssl

    # For development/testing, create self-signed certificate
    # In production, replace with Let's Encrypt certificate
    if [ ! -f ssl/tradeos.crt ] || [ ! -f ssl/tradeos.key ]; then
        openssl req -x509 -newkey rsa:4096 -keyout ssl/tradeos.key -out ssl/tradeos.crt \
            -days 365 -nodes -subj "/CN=tradeos.local" 2>/dev/null || true
        echo "⚠️  Created self-signed certificate for development"
        echo "   For production, configure Let's Encrypt in Nginx"
    fi

    chmod 600 ssl/tradeos.key
    echo "✓ SSL configured"
}

# Create signer configuration directory
setup_signer() {
    echo "✓ Setting up signer configuration..."

    mkdir -p signer_keys
    chmod 700 signer_keys

    echo "⚠️  Signer configuration directory created at: signer_keys/"
    echo "   Place your keys here:"
    echo "   - id.json (Solana keypair from solana-cli)"
    echo "   - evm.key (EVM private key in hex format)"
    echo ""
    echo "   Keys will be mounted into the signer container."
    echo "   NEVER commit this directory to git."
}

# Build Docker images
build_images() {
    echo "✓ Building Docker images..."

    docker-compose build --pull

    echo "✓ Docker images built successfully"
}

# Initialize database
init_database() {
    echo "✓ Initializing database..."

    # Start PostgreSQL first
    docker-compose up -d postgres

    # Wait for PostgreSQL to be ready
    echo "  Waiting for PostgreSQL..."
    for i in {1..30}; do
        if docker-compose exec -T postgres pg_isready -U tradeos -d tradeos >/dev/null 2>&1; then
            echo "  ✓ PostgreSQL ready"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "❌ PostgreSQL failed to start"
            exit 1
        fi
        sleep 1
    done

    echo "✓ Database initialized"
}

# Create necessary directories
create_directories() {
    echo "✓ Creating necessary directories..."

    mkdir -p data logs backups
    chmod 755 data logs backups

    echo "✓ Directories created"
}

# Start all services
start_services() {
    echo "✓ Starting all services..."

    docker-compose up -d

    echo "  Waiting for services to be ready..."
    sleep 5

    # Check health
    for i in {1..30}; do
        if curl -sf http://localhost:8420/health >/dev/null 2>&1; then
            echo "  ✓ All services ready"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "⚠️  Services may still be starting. Check logs with: docker-compose logs -f"
        fi
        sleep 1
    done

    echo "✓ Services started"
}

# Display information
show_info() {
    echo ""
    echo "✅ TradeOS Production Deployment Complete!"
    echo ""
    echo "Access points:"
    echo "  Web Dashboard: https://localhost/  (or your domain)"
    echo "  API Health: https://localhost/health"
    echo ""
    echo "Useful commands:"
    echo "  docker-compose logs -f              # View logs"
    echo "  docker-compose exec tradeos sh      # SSH into app"
    echo "  docker-compose exec postgres psql -U tradeos -d tradeos  # SQL shell"
    echo "  docker-compose restart tradeos      # Restart app"
    echo "  ./scripts/backup.sh                 # Backup database"
    echo ""
    echo "Next steps:"
    echo "  1. Update Nginx configuration with your domain"
    echo "  2. Configure DNS to point to this server"
    echo "  3. Set up Let's Encrypt certificates for HTTPS"
    echo "  4. Test paper trading thoroughly"
    echo "  5. Only then enable live trading (if desired)"
    echo ""
    echo "⚠️  IMPORTANT: Keep .env secure. It contains sensitive credentials."
    echo ""
}

# Main execution
main() {
    check_prerequisites
    create_directories
    setup_environment
    setup_ssl
    setup_signer
    build_images
    init_database
    start_services
    show_info
}

main "$@"
