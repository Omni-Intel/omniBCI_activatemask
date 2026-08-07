@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build OmniBCI V16 Windows EXE

echo ============================================================
echo   OmniBCI V16 - Windows EXE builder (recommended ONEDIR)
echo ============================================================
echo.

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
    echo Install Python 3.12 x64 from python.org, then run this file again.
    pause
    exit /b 1
)

echo Using build Python: %PY_CMD%

if not exist ".buildvenv\Scripts\python.exe" (
    echo Creating isolated build environment...
    %PY_CMD% -m venv ".buildvenv"
    if errorlevel 1 goto :fail
)

set "BPY=%CD%\.buildvenv\Scripts\python.exe"

echo Updating build tools...
"%BPY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

echo Installing OmniBCI runtime dependencies...
"%BPY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo Installing PyInstaller...
"%BPY%" -m pip install "pyinstaller>=6.10,<7"
if errorlevel 1 goto :fail

echo Verifying modules...
"%BPY%" -c "import serial,numpy,scipy,pyqtgraph,PySide6,bleak; print('Runtime modules: OK')"
if errorlevel 1 goto :fail

echo.
echo Building OmniBCI_V16.exe ...
if exist build rmdir /s /q build
if exist "dist\OmniBCI_V16" rmdir /s /q "dist\OmniBCI_V16"
"%BPY%" -m PyInstaller --noconfirm --clean OmniBCI_V16.spec
if errorlevel 1 goto :fail

if not exist "dist\OmniBCI_V16\OmniBCI_V16.exe" (
    echo ERROR: PyInstaller completed but EXE was not found.
    goto :fail
)

mkdir "dist\OmniBCI_V16\recordings" >nul 2>&1
if exist firmware xcopy /E /I /Y firmware "dist\OmniBCI_V16\firmware" >nul
copy /Y README.md "dist\OmniBCI_V16\README.md" >nul
copy /Y VERSION_AND_QUICK_START.txt "dist\OmniBCI_V16\VERSION_AND_QUICK_START.txt" >nul
copy /Y FIRMWARE_COMPATIBILITY.txt "dist\OmniBCI_V16\FIRMWARE_COMPATIBILITY.txt" >nul
copy /Y EXE_BUILD_NOTES.txt "dist\OmniBCI_V16\EXE_BUILD_NOTES.txt" >nul
copy /Y VALIDATION_REPORTS.txt "dist\OmniBCI_V16\VALIDATION_REPORTS.txt" >nul

echo.
echo ============================================================
echo BUILD SUCCESS
echo.
echo Send the WHOLE folder below to another Windows computer:
echo   %CD%\dist\OmniBCI_V16
echo.
echo Run:
echo   OmniBCI_V16.exe
echo ============================================================
explorer "%CD%\dist\OmniBCI_V16"
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Scroll up to the first ERROR line.
pause
exit /b 1
