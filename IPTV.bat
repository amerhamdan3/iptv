@echo off
setlocal
title IPTV  -  close this window to stop
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem --- Find Python on PATH (python, then the py launcher).
set "PY="
for %%I in (python.exe) do if exist "%%~$PATH:I" set "PY=python"
if not defined PY for %%I in (py.exe) do if exist "%%~$PATH:I" set "PY=py"
if not defined PY (
    echo.
    echo   Python was not found.
    echo   Install it from https://python.org and tick "Add to PATH".
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo   No .env file yet - copy .env.example to .env and put your
    echo   IPTV provider details in it, then run this again.
    echo.
    pause
    exit /b 1
)

set "URL=http://127.0.0.1:8000"

rem --- If it's already running, just open the browser and get out of the way.
powershell -NoProfile -Command ^
  "try{$null=Invoke-WebRequest '%URL%/api/status' -TimeoutSec 2 -UseBasicParsing;exit 0}catch{exit 1}"
if %errorlevel% equ 0 (
    echo IPTV is already running - opening browser.
    start "" "%URL%"
    timeout /t 2 >nul
    exit /b
)

rem --- Open the browser as soon as the server answers, then keep serving here.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i -lt 120;$i++){try{$null=Invoke-WebRequest '%URL%/api/status' -TimeoutSec 2 -UseBasicParsing;Start-Process '%URL%';break}catch{Start-Sleep -Milliseconds 500}}"

echo.
echo   ============================================================
echo      IPTV is starting...
echo.
echo      Your browser will open by itself in a few seconds.
echo      The very first run takes about a minute while the
echo      channel list downloads.
echo.
echo      KEEP THIS WINDOW OPEN while you are watching.
echo      Close it when you are done to stop IPTV.
echo   ============================================================
echo.

%PY% app.py

echo.
echo IPTV has stopped.
timeout /t 5 >nul
