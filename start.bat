@echo off
echo ======================================
echo   EKO Reconciliation App
echo ======================================
echo.
echo Starting backend on  - http://localhost:8001
echo Starting frontend on - http://localhost:3000
echo.
echo Default login: admin / Admin@1234
echo.
echo Close this window to stop both servers
echo ======================================

start "EKO Recon Backend" cmd /k "cd backend && python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload"
timeout /t 3 /nobreak >nul
start "EKO Recon Frontend" cmd /k "cd frontend && npm run dev"
