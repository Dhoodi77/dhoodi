#!/bin/bash
# Stop the demo server

pkill -f "python3 -m tradeos.main" || true
echo "✓ TradeOS demo stopped"
