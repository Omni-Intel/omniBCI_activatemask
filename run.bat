@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%CD%\products\stm32_bci\run.bat" %*
exit /b %ERRORLEVEL%
