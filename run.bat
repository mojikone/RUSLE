@echo off
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"

REM ── Locate Python (local venv preferred) ─────────────────────────────────
if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PY=%ROOT%.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

if "%1"=="setup" goto setup
if "%1"=="01"    goto step01
if "%1"=="02"    goto step02
if "%1"=="03"    goto step03
if "%1"=="all"   goto all

REM ── No argument: auto-setup on first run, show usage otherwise ───────────
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo  No virtual environment found — running first-time setup ...
    echo.
    goto setup
)

echo.
echo  RUSLE Sediment Yield Pipeline
echo  ─────────────────────────────
echo  Usage:  run.bat [setup ^| 01 ^| 02 ^| 03 ^| all]
echo.
echo    setup  Create .venv and install requirements.txt
echo    01     Download all input data  (internet required)
echo    02     Compute RUSLE factors + export CSV
echo    03     Generate maps  (satellite basemap, masked to catchments)
echo    all    Run steps 01 ^> 02 ^> 03 in sequence
echo.
echo  Quick start:
echo    run.bat setup
echo    run.bat all
echo.
goto end

:setup
echo ══ Setup: Creating virtual environment ════════════════
python -m venv "%ROOT%.venv"
if errorlevel 1 ( echo [ERROR] python not found — install Python 3.10+ first & goto end )
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt" --quiet
echo.
echo  Setup complete.  Now run:  run.bat all
goto end

:all
:step01
echo.
echo ══ Step 1: Download Data ══════════════════════════════
"%PY%" "%ROOT%Scripts\01_download.py"
if errorlevel 1 ( echo [ERROR] Step 1 failed & goto end )
if "%1"=="01" goto end

:step02
echo.
echo ══ Step 2: Compute RUSLE ══════════════════════════════
"%PY%" "%ROOT%Scripts\02_compute.py"
if errorlevel 1 ( echo [ERROR] Step 2 failed & goto end )
if "%1"=="02" goto end

:step03
echo.
echo ══ Step 3: Generate Maps ══════════════════════════════
"%PY%" "%ROOT%Scripts\03_maps.py"
if errorlevel 1 ( echo [ERROR] Step 3 failed & goto end )

:end
echo.
pause
endlocal
