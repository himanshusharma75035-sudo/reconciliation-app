@echo off
title EKO Recon - Backend (port 8001)
rem Local convenience launcher: runs the recon backend on port 8001 because
rem port 8000 is taken by another app (OpenFP&A) on this machine.
rem The frontend proxies /api -> :8001 via the gitignored frontend/.env.local.
rem Machine-specific — not intended for commit.
rem %~dp0 = this script's directory, so the repo can live anywhere.
cd /d "%~dp0..\backend"
echo Starting EKO Reconciliation Backend on port 8001...
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8001
pause
