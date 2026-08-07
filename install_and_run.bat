@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title OmniBCI V16 - first time setup

echo [OmniBCI V16] Preparing an isolated Python environment...
echo.

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.12"
    if not defined PY_CMD py -3.11 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.11"
    if not defined PY_CMD py -3 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3"
)
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo ERROR: Python was not found.
    echo Install 64-bit Python 3.12 or 3.11, then run this file again.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Using: %PY_CMD%
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    %PY_CMD% -m venv ".venv"
    if errorlevel 1 (
        echo ERROR: Failed to create .venv.
        pause
        exit /b 1
    )
)

set "VPY=%CD%\.venv\Scripts\python.exe"
echo Virtual Python: %VPY%

echo.
echo Updating pip/setuptools/wheel ...
"%VPY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :pipfail

echo.
echo Installing live-acquisition dependencies ...
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto :pipfail

echo.
echo Verifying core modules ...
"%VPY%" -c "import serial,numpy,scipy,pyqtgraph,PySide6,bleak; print('Core dependencies: OK')"
if errorlevel 1 goto :pipfail

echo.
echo Starting OmniBCI V16 ...
"%VPY%" ads1299_eeg_gui_native.py
set "APP_RC=%ERRORLEVEL%"
if not "%APP_RC%"=="0" (
    echo.
    echo GUI exited with code %APP_RC%.
    pause
)
exit /b %APP_RC%

:pipfail
echo.
echo ERROR: dependency installation failed.
echo The live GUI does NOT require mne or pyedflib.
echo If the error mentions pyedflib, you are using an old package list.
echo Delete .venv and run this installer again.
pause
exit /b 1
