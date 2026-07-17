@echo off
cd /d "%~dp0"
echo Installing Python dependencies...
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo pip install failed. Check Python installation / network.
  pause
  exit /b 1
)
echo.
echo Starting ADS1299 Native Python GUI...
py -3 ads1299_eeg_gui_native.py
pause
