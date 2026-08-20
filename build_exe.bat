@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Build OmniBCI V19 Windows EXE

set "APP_NAME=OmniBCI_V19"
set "SPEC_FILE=OmniBCI_V19.spec"
set "MIRROR=https://mirrors.sustech.edu.cn/pypi/web/simple"
set "BUNDLE_EXPORTS=1"
if /i "%~1"=="--no-exports" set "BUNDLE_EXPORTS=0"
if /i "%~1"=="--slim" set "BUNDLE_EXPORTS=0"

echo ============================================================
echo   OmniBCI V19 - Windows EXE builder (ONEDIR)
if "%BUNDLE_EXPORTS%"=="1" (
    echo   Mode: FULL  - includes BDF/FIF export (mne + pyedflib)
) else (
    echo   Mode: SLIM  - live acquisition only
)
echo ============================================================
echo.

REM ---------------------------------------------------------------
REM Step 1: choose the build Python.
REM Prefer the project .venv (uv-locked deps, already includes
REM mne/pyedflib). Fallback: isolated .buildvenv from requirements.
REM ---------------------------------------------------------------
set "BPY="
if exist ".venv\Scripts\python.exe" (
    echo Checking project .venv ...
    ".venv\Scripts\python.exe" -c "import serial,numpy,scipy,pyqtgraph,PySide6,bleak,websockets" >nul 2>&1
    if not errorlevel 1 set "BPY=%CD%\.venv\Scripts\python.exe"
)

if defined BPY if "%BUNDLE_EXPORTS%"=="1" (
    "!BPY!" -c "import mne,pyedflib" >nul 2>&1
    if errorlevel 1 (
        echo [WARN] .venv is missing mne/pyedflib - will use .buildvenv instead.
        set "BPY="
    )
)

if defined BPY (
    where uv >nul 2>&1
    if errorlevel 1 (
        echo [WARN] uv not found; cannot add PyInstaller to the uv-managed .venv.
        set "BPY="
    )
)

if defined BPY (
    echo Using project virtual environment: !BPY!
    echo Ensuring PyInstaller ...
    uv pip install --python "!BPY!" "pyinstaller>=6.10,<7"
    if errorlevel 1 goto :fail
    goto :havepython
)

REM ---------------------------------------------------------------
REM Fallback: isolated .buildvenv built from requirements files.
REM ---------------------------------------------------------------
set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.12"
    if not defined PY_CMD py -3.11 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.11"
)
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; exit(0 if sys.version_info[:2] in [(3,11),(3,12)] else 1)" >nul 2>&1 && set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo ERROR: Python 3.12 or 3.11 64-bit is required only on the BUILD computer.
    echo Target computers do NOT need Python after the EXE is built.
    goto :fail
)

echo Using build Python: %PY_CMD%
if not exist ".buildvenv\Scripts\python.exe" (
    echo Creating isolated build environment ...
    %PY_CMD% -m venv ".buildvenv"
    if errorlevel 1 goto :fail
)
set "BPY=%CD%\.buildvenv\Scripts\python.exe"

echo Updating build tools ...
"!BPY!" -m pip install --upgrade pip setuptools wheel -i %MIRROR%
if errorlevel 1 goto :fail

echo Installing OmniBCI runtime dependencies ...
"!BPY!" -m pip install -r requirements.txt -i %MIRROR%
if errorlevel 1 goto :fail

if "%BUNDLE_EXPORTS%"=="1" (
    echo Installing export dependencies ^(mne, pyedflib^) ...
    "!BPY!" -m pip install -r requirements_optional_export.txt -i %MIRROR%
    if errorlevel 1 goto :fail
)

echo Installing PyInstaller ...
"!BPY!" -m pip install "pyinstaller>=6.10,<7" -i %MIRROR%
if errorlevel 1 goto :fail

:havepython
echo.
echo Verifying modules ...
if "%BUNDLE_EXPORTS%"=="1" (
    "!BPY!" -c "import serial,numpy,scipy,pyqtgraph,PySide6,bleak,websockets,mne,pyedflib,app_diagnostics,onmibci_stream,onmibci_sdk,onmibci_ble_protocol; print('Runtime modules: OK (full)')"
) else (
    "!BPY!" -c "import serial,numpy,scipy,pyqtgraph,PySide6,bleak,websockets,app_diagnostics,onmibci_stream,onmibci_sdk,onmibci_ble_protocol; print('Runtime modules: OK (slim)')"
)
if errorlevel 1 goto :fail

echo.
echo Building %APP_NAME%.exe ...
set "OMNIBCI_BUNDLE_EXPORTS=%BUNDLE_EXPORTS%"
if exist build rmdir /s /q build
if exist "dist\%APP_NAME%" rmdir /s /q "dist\%APP_NAME%"
"!BPY!" -m PyInstaller --noconfirm --clean %SPEC_FILE%
if errorlevel 1 goto :fail

if not exist "dist\%APP_NAME%\%APP_NAME%.exe" (
    echo ERROR: PyInstaller completed but EXE was not found.
    goto :fail
)

echo Creating runtime directories ...
mkdir "dist\%APP_NAME%\recordings\bin" >nul 2>&1
mkdir "dist\%APP_NAME%\recordings\bdf" >nul 2>&1
mkdir "dist\%APP_NAME%\recordings\fif" >nul 2>&1
mkdir "dist\%APP_NAME%\logs" >nul 2>&1

REM The release package intentionally contains only the executable runtime.
REM Source, firmware, SDK, build notes and project documentation stay out.

echo.
echo ============================================================
echo BUILD SUCCESS
echo.
echo Send the WHOLE folder below to another Windows computer:
echo   %CD%\dist\%APP_NAME%
echo.
echo Run:
echo   %APP_NAME%.exe
echo ============================================================
explorer "%CD%\dist\%APP_NAME%"
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Scroll up to the first ERROR line.
pause
exit /b 1
