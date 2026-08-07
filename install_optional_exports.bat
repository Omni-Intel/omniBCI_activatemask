@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install_and_run.bat first.
    pause
    exit /b 1
)
set "VPY=%CD%\.venv\Scripts\python.exe"
"%VPY%" -m pip install -r requirements_optional_export.txt
if errorlevel 1 (
    echo.
    echo Optional export packages failed to install.
    echo Live USB/BLE acquisition is still fully usable.
    echo For pyEDFlib build errors, recreate .venv with 64-bit Python 3.12.
    pause
    exit /b 1
)
echo Optional BDF/MNE export dependencies installed.
pause
