@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM run_floor_detector.bat
REM
REM Launcher for Tuned_Floor_Detector.py.
REM
REM Repo layout this assumes (confirmed by user):
REM   C:\Users\Knutb\RuView\                          <- repo root, this .bat + Tuned_Floor_Detector.py live here
REM   C:\Users\Knutb\RuView\archive\v1\src\sensing\rssi_collector.py  <- the real v1 package
REM
REM Tuned_Floor_Detector.py imports "from v1.src.sensing.rssi_collector
REM import WindowsWifiCollector" -- that only resolves if the "archive"
REM folder (not the repo root) is on PYTHONPATH, since "v1" lives one
REM level inside archive\, not at the repo root. This script sets that
REM PYTHONPATH for the duration of the run only -- it does not touch
REM any system-wide environment variable, and it does not modify the
REM .py files themselves.
REM
REM Python environment: plain venv / system Python, no conda involved.
REM   1. Use python on PATH if present (activate your venv first, or
REM      this just uses system Python).
REM   2. Fall back to the "py" launcher if "python" isn't found.
REM   3. Verify numpy/matplotlib are importable; auto-install via pip
REM      if missing.
REM   4. Launch Tuned_Floor_Detector.py with PYTHONPATH set to
REM      ...\RuView\archive.
REM ============================================================

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "ARCHIVE_DIR=%SCRIPT_DIR%archive"

echo ============================================================
echo  RuView Floor Detector Launcher
echo ============================================================
echo.

REM --- Step 1: find a usable Python -----------------------------------
set "PYTHON_CMD="

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_CMD=python"
    goto :found_python
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON_CMD=py"
    goto :found_python
)

echo [!] No Python found on PATH.
echo     Activate your virtual environment first, or install Python,
echo     then re-run this script.
goto :end_fail

:found_python
echo [OK] Using Python command: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

REM --- Step 2: dependency check -----------------------------------
echo Checking required packages ^(numpy, matplotlib^)...

%PYTHON_CMD% -c "import numpy" >nul 2>nul
set "NEED_NUMPY=%ERRORLEVEL%"

%PYTHON_CMD% -c "import matplotlib" >nul 2>nul
set "NEED_MATPLOTLIB=%ERRORLEVEL%"

if "%NEED_NUMPY%"=="0" if "%NEED_MATPLOTLIB%"=="0" (
    echo [OK] numpy and matplotlib are already installed.
    goto :check_layout
)

echo [!] Missing package^(s^) detected:
if not "%NEED_NUMPY%"=="0" echo     - numpy
if not "%NEED_MATPLOTLIB%"=="0" echo     - matplotlib
echo.
echo Installing missing packages via pip...
%PYTHON_CMD% -m pip install numpy matplotlib
if %ERRORLEVEL% NEQ 0 (
    echo [!] pip install failed. Please install numpy/matplotlib manually.
    goto :end_fail
)
echo [OK] Packages installed.
echo.

:check_layout
REM --- Step 3: confirm the detector script and the v1 package exist ---
if not exist "%SCRIPT_DIR%Tuned_Floor_Detector.py" (
    echo [!] Tuned_Floor_Detector.py not found in %SCRIPT_DIR%
    echo     Make sure this .bat file sits next to it.
    goto :end_fail
)

if not exist "%ARCHIVE_DIR%\v1\src\sensing\rssi_collector.py" (
    echo [!] v1\src\sensing\rssi_collector.py not found under:
    echo       %ARCHIVE_DIR%
    echo     Expected layout: %SCRIPT_DIR%archive\v1\src\sensing\rssi_collector.py
    echo     The import will fail unless this path is correct.
    goto :end_fail
)
echo [OK] Found v1 package under archive\.
echo.

REM --- Step 4: launch with PYTHONPATH pointed at archive\ --------------
echo ============================================================
echo  Launching Tuned_Floor_Detector.py
echo  (PYTHONPATH includes: %ARCHIVE_DIR%)
echo ============================================================
echo.

set "PYTHONPATH=%ARCHIVE_DIR%;%PYTHONPATH%"
%PYTHON_CMD% "%SCRIPT_DIR%Tuned_Floor_Detector.py"
set "RUN_RESULT=%ERRORLEVEL%"

echo.
if %RUN_RESULT% NEQ 0 (
    echo [!] Script exited with an error ^(code %RUN_RESULT%^).
) else (
    echo [OK] Script exited normally.
)
goto :end_ok

:end_fail
echo.
echo ============================================================
echo  Launcher stopped before running the detector.
echo ============================================================
pause
exit /b 1

:end_ok
pause
exit /b 0
