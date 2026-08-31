@echo off
setlocal
cd /d "%~dp0"
if not exist "app\.venv\Scripts\python.exe" (
    echo STM32 environment missing. Follow setup in this product's README.md.
    exit /b 1
)
"app\.venv\Scripts\python.exe" -B "%~dp0run_stm32.py" %*
exit /b %errorlevel%
