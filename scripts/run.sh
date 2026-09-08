#!/usr/bin/env bash
# Run TradeOS. Expects configuration in .env (see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m tradeos.main
