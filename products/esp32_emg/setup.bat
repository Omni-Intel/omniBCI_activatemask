@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>&1
if errorlevel 1 (
    echo uv is required. See README.md for setup instructions.
    exit /b 1
)
uv sync --locked --all-extras --project app
exit /b %errorlevel%
