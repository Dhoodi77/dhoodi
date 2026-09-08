#!/bin/bash
# TradeOS Demo - Local deployment with paper trading
# Start with: bash scripts/demo.sh
# Access: http://localhost:8420 (computer) or http://<IP>:8420 (iPhone)

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "🟢 Starting TradeOS DEMO (Paper Trading)"
echo ""

# Detect local IP
LOCAL_IP=$(bash scripts/get-local-ip.sh 2>/dev/null || echo "192.168.x.x")
if [ "$LOCAL_IP" = "127.0.0.1" ]; then
    LOCAL_IP="(check: ifconfig / ip addr)"
fi

echo "════════════════════════════════════════════════"
echo "   TradeOS DEMO Configuration"
echo "════════════════════════════════════════════════"
echo ""
echo "   Mode:           PAPER TRADING"
echo "   Start Balance:  \$10,000"
echo "   Live Trading:   DISABLED"
echo ""
echo "   Dashboard URL:  http://localhost:8420"
echo "   iPhone URL:     http://$LOCAL_IP:8420"
echo ""
echo "   Token:          demo_token_tradeos_2024"
echo ""
echo "════════════════════════════════════════════════"
echo ""

# Create data directory
mkdir -p data logs

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Install Python 3.11+ and try again."
    exit 1
fi

# Activate venv if exists
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✓ Virtual environment activated"
fi

# Install dependencies (if needed)
if ! python3 -c "import fastapi" 2>/dev/null; then
    echo "📦 Installing dependencies..."
    pip install -e . -q
fi

# Check for .env.demo
if [ ! -f ".env.demo" ]; then
    echo "❌ .env.demo not found. Creating from template..."
    if [ ! -f ".env.production.template" ]; then
        echo "❌ .env.production.template not found either."
        echo "   Expected to find template in repo root."
        exit 1
    fi
    cp .env.production.template .env.demo
fi

# Copy demo env to .env for the app to use
cp .env.demo .env

# Cleanup old database (optional - comment out to keep history)
# rm -f data/tradeos_demo.db

echo ""
echo "🚀 Launching TradeOS..."
echo ""
echo "   Access dashboard at: http://localhost:8420"
echo "   Authentication token: demo_token_tradeos_2024"
echo ""
echo "   Press Ctrl+C to stop"
echo ""

# Set demo mode explicitly
export TRADEOS_DEMO_MODE=true
export TRADEOS_MODE=paper

# Run the app
python3 -m tradeos.main 2>&1 | tee -a logs/tradeos.log &
APP_PID=$!

echo ""
echo "   PID: $APP_PID"
echo ""

# Wait for server to start
echo "⏳ Waiting for server to start..."
sleep 2

# Check if started successfully
if ! curl -s http://localhost:8420/api/health > /dev/null 2>&1; then
    echo ""
    echo "⚠️  Server may not be fully started. Trying again..."
    sleep 3
fi

if curl -s http://localhost:8420/api/health > /dev/null 2>&1; then
    echo "✓ Server running on http://localhost:8420"
else
    echo "❌ Server failed to start. Check logs:"
    tail -20 logs/tradeos.log
    kill $APP_PID 2>/dev/null
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════"
echo "   🟢 DEMO READY"
echo ""
echo "   1. Open browser: http://localhost:8420"
echo "   2. Enter token: demo_token_tradeos_2024"
echo "   3. Watch agents discover and analyze tokens"
echo "   4. Paper trades execute automatically"
echo ""
echo "   For iPhone: http://$LOCAL_IP:8420"
echo ""
echo "════════════════════════════════════════════════"
echo ""

# Keep the process alive
wait $APP_PID
