@echo off
title NIA Performance Center — First Time Setup
cd /D "%~dp0"
echo.
echo  ============================================================
echo   NIA Performance Center — First Time Setup
echo  ============================================================
echo.

:: ── Check Python ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://python.org
    pause & exit /b 1
)
echo [OK] Python found.

:: ── Check Node.js ─────────────────────────────────────────────────────────────
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org
    pause & exit /b 1
)
echo [OK] Node.js found.

:: ── Create Python venv ────────────────────────────────────────────────────────
if not exist "backend\venv\Scripts\python.exe" (
    echo [1/5] Creating Python virtual environment...
    python -m venv backend\venv
    echo [OK] Virtual environment created.
) else (
    echo [1/5] Python venv already exists.
)

:: ── Install Python packages ───────────────────────────────────────────────────
echo [2/5] Installing Python packages...
backend\venv\Scripts\pip.exe install -r backend\requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python packages.
    pause & exit /b 1
)
echo [OK] Python packages installed.

:: ── Install main frontend npm packages ────────────────────────────────────────
echo [3/5] Installing main frontend packages...
cd frontend
call npm install --silent
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install main frontend packages.
    cd ..
    pause & exit /b 1
)
cd ..
echo [OK] Main frontend packages installed.

:: ── Install webportal frontend npm packages ───────────────────────────────────
echo [4/5] Installing webportal frontend packages...
cd webportal\frontend
call npm install --silent
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install webportal frontend packages.
    cd ..\..
    pause & exit /b 1
)
cd ..\..
echo [OK] Webportal frontend packages installed.

:: ── Build both frontends ──────────────────────────────────────────────────────
echo [5/5] Building both React frontends...
cd webportal\frontend
call npm run build --silent
cd ..\..
cd frontend
call npm run build --silent
cd ..
echo [OK] Frontends built.

echo.
echo  ============================================================
echo   Setup complete! Run start.bat to launch the app.
echo  ============================================================
echo.
pause
