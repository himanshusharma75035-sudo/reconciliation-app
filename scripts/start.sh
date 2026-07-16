#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
echo "======================================"
echo "  EKO Reconciliation App"
echo "======================================"
echo ""
echo "Starting backend on  → http://localhost:8000"
echo "Starting frontend on → http://localhost:3000"
echo ""
echo "Default login: admin / Admin@1234"
echo ""
echo "Press Ctrl+C to stop both servers"
echo "======================================"

# Start backend
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!
cd ..

# Start frontend
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

# Handle shutdown
trap "echo 'Shutting down...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM

wait $BACKEND_PID $FRONTEND_PID
