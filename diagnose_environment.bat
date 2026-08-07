@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo ===== OmniBCI V16 environment diagnostic =====
echo Folder: %CD%
echo.
where py 2>nul
where python 2>nul
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys,platform; print('Python:',sys.version); print('Executable:',sys.executable); print('Windows:',platform.platform())"
  ".venv\Scripts\python.exe" -c "import serial,scipy,numpy,bleak,PySide6,pyqtgraph; print('Core packages: OK'); print('pyserial:',serial.__version__)"
) else (
  echo .venv not found. Run install_and_run.bat first.
)
echo.
echo ===== End =====
pause
