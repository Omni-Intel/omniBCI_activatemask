@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Build OmniBCI V19 Windows EXE
set "APP_NAME=OmniBCI_V19"
set "OMNIBCI_BUNDLE_EXPORTS=1"
set "INTERACTIVE=1"

:args
if "%~1"=="" goto :prepare
if /i "%~1"=="--slim" goto :slim
if /i "%~1"=="--no-exports" goto :slim
if /i "%~1"=="--non-interactive" goto :noninteractive
echo ERROR: Unknown argument "%~1"
exit /b 2
:slim
set "OMNIBCI_BUNDLE_EXPORTS=0"
shift
goto :args
:noninteractive
set "INTERACTIVE=0"
shift
goto :args

:prepare
echo OmniBCI V19 EEG - Windows ONEDIR builder
if "%OMNIBCI_BUNDLE_EXPORTS%"=="1" echo Mode: FULL ^(BDF/FIF exports included^)
if "%OMNIBCI_BUNDLE_EXPORTS%"=="0" echo Mode: SLIM ^(no BDF/FIF exports^)
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is required on the build computer. See EXE_BUILD_NOTES.txt.
    goto :fail
)

REM Never install build tools into the validated acquisition environment.
set "UV_PROJECT_ENVIRONMENT=%CD%\.buildvenv"
set "BPY=%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe"
uv sync --frozen --no-dev
if errorlevel 1 goto :fail
uv pip install --python "%BPY%" "pyinstaller==6.22.2"
if errorlevel 1 goto :fail
"%BPY%" -c "import struct; assert struct.calcsize('P') == 8, '64-bit Python required'; import serial,numpy,scipy,pyqtgraph,PySide6,bleak,websockets,mne,pyedflib"
if errorlevel 1 goto :fail

REM Unique output paths preserve recordings and logs beside older releases.
set "BUILD_ID="
for /f %%I in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N')"') do set "BUILD_ID=%%I"
if not defined BUILD_ID goto :fail
set "OUTPUT=%CD%\dist\%BUILD_ID%"
REM Avoid collecting incompatible DLLs from unrelated tools on the host PATH.
set "PATH=%SystemRoot%\System32;%SystemRoot%"
set "PYTHONPATH="
"%BPY%" -m PyInstaller --noconfirm --clean --distpath "%OUTPUT%" --workpath "%CD%\build\%BUILD_ID%" OmniBCI_V19.spec
if errorlevel 1 goto :fail
if not exist "%OUTPUT%\%APP_NAME%\%APP_NAME%.exe" goto :fail

echo.
echo BUILD SUCCESS. Send the WHOLE directory, including _internal:
echo "%OUTPUT%\%APP_NAME%"
echo Build output is local only. Do not commit the EXE or its ZIP.
if "%INTERACTIVE%"=="1" pause
exit /b 0

:fail
echo.
echo BUILD FAILED. See the first ERROR above. Existing releases were not deleted.
if "%INTERACTIVE%"=="1" pause
exit /b 1
