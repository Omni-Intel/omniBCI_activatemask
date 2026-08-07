@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo OmniBCI environment is not installed yet.
    echo Starting first-time setup...
    call install_and_run.bat
    exit /b
)
"%CD%\.venv\Scripts\python.exe" ads1299_eeg_gui_native.py
set "APP_RC=%ERRORLEVEL%"
if not "%APP_RC%"=="0" pause
exit /b %APP_RC%
