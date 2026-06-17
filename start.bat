@echo off
REM K.A.S.H. Diagnostics v2.1 — Start Script (Windows)

cd /d "%~dp0"

REM Check for Python
where python >nul 2>&1 || (
    echo [K.A.S.H.] Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

REM Install dependencies if needed
python -c "import fastapi" 2>nul || (
    echo [K.A.S.H.] Installing dependencies...
    pip install -r requirements.txt
)

echo.
echo   ══════════════════════════════════════════════════
echo    K.A.S.H. DIAGNOSTICS v2.1
echo    Open browser to: http://localhost:8000
echo   ══════════════════════════════════════════════════
echo.

python kash_diagnostics.py %*
pause
