@echo off
setlocal EnableExtensions
call "%~dp0app\run.bat"
exit /b %ERRORLEVEL%
