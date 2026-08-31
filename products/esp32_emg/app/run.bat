@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo OmniEMG environment is not installed yet.
    call "..\setup.bat"
    if errorlevel 1 exit /b 1
)
cd /d "%~dp0"
"%CD%\.venv\Scripts\python.exe" ads1299_eeg_gui_native.py
set "APP_RC=%ERRORLEVEL%"
if not "%APP_RC%"=="0" pause
exit /b %APP_RC%
