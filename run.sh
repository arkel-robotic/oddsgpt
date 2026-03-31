#!/usr/bin/env bash
# ─────────────────────────────────────────────────
#  OddsGPT — Setup & Launch Script
# ─────────────────────────────────────────────────

set -e
echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║       OddsGPT AI Predictor        ║"
echo "  ║    Sports Betting AI Assistant    ║"
echo "  ╚═══════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python3 not found. Please install Python 3.10+"
  exit 1
fi

# Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt --quiet

# Launch
echo ""
echo "🚀 Starting OddsGPT server..."
echo "🌐 Open your browser at: http://localhost:8000"
echo ""
echo "  Example queries:"
echo "  • football - Ukraine vs Albania, how should I play it?"
echo "  • basketball - Lakers vs Warriors, best bets?"
echo "  • tennis - Djokovic vs Sinner, predictions?"
echo ""
echo "  Press Ctrl+C to stop"
echo ""

cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
