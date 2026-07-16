#!/bin/bash
set -e
cd "$(dirname "$0")/.." || exit 1
echo "======================================"
echo "  EKO Reconciliation App - Setup"
echo "======================================"

# Backend
echo ""
echo "📦 Installing Python dependencies..."
cd backend
pip install -r requirements.txt --break-system-packages --quiet
cd ..

# Frontend
echo ""
echo "📦 Installing Node dependencies..."
cd frontend
npm install --silent
cd ..

echo ""
echo "✅ Setup complete!"
echo ""
echo "Run ./scripts/start.sh to launch the app"
