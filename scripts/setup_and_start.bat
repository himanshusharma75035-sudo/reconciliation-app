@echo off
title EKO Reconciliation - Setup ^& Start
color 0A
echo.
echo  ==========================================
echo    EKO Reconciliation App - First Setup
echo  ==========================================
echo.

:: Get the directory of this batch file
set "APP_DIR=%~dp0..\"
set "BACKEND_DIR=%APP_DIR%backend"
set "FRONTEND_DIR=%APP_DIR%frontend"

echo  App folder: %APP_DIR%
echo.

:: ── Check Python ──────────────────────────────
echo  [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Please install Python 3.10+ from python.org
    pause
    exit /b 1
)
python --version
echo  Python OK
echo.

:: ── Check Node ────────────────────────────────
echo  [2/4] Checking Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Node.js not found. Please install Node.js 18+ from nodejs.org
    pause
    exit /b 1
)
node --version
echo  Node OK
echo.

:: ── Install Python deps ────────────────────────
echo  [3/4] Installing Python packages (this may take 2-3 minutes first time)...
cd /d "%BACKEND_DIR%"
pip install fastapi uvicorn sqlalchemy python-jose passlib python-multipart pandas openpyxl pdfplumber aiofiles bcrypt --quiet
if errorlevel 1 (
    echo  WARNING: Some packages may have failed. Trying with --break-system-packages...
    pip install fastapi uvicorn sqlalchemy python-jose passlib python-multipart pandas openpyxl pdfplumber aiofiles bcrypt --break-system-packages --quiet
)
echo  Python packages OK
echo.

:: ── Install Node deps ──────────────────────────
echo  [4/4] Installing Node packages (this may take 1-2 minutes first time)...
cd /d "%FRONTEND_DIR%"
call npm install --silent
if errorlevel 1 (
    echo  ERROR: npm install failed
    pause
    exit /b 1
)
echo  Node packages OK
echo.

:: ── Start servers ─────────────────────────────
echo  ==========================================
echo    Starting EKO Reconciliation App...
echo  ==========================================
echo.
echo  Backend  → http://localhost:8001
echo  Frontend → http://localhost:3000
echo.
echo  Login: admin / Admin@1234
echo.
echo  Starting backend server...
cd /d "%BACKEND_DIR%"
start "EKO Backend" cmd /k "python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload"

echo  Waiting for backend to start...
timeout /t 5 /nobreak >nul

echo  Starting frontend server...
cd /d "%FRONTEND_DIR%"
start "EKO Frontend" cmd /k "npm run dev"

echo  Waiting for frontend to start...
timeout /t 8 /nobreak >nul

echo  Opening browser...
start "" "http://localhost:3000"

echo.
echo  Both servers are running in separate windows.
echo  Close those windows to stop the servers.
echo.
pause
