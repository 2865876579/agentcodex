@echo off
setlocal

set "ROOT=%~dp0"
set "SERVER_DIR=%ROOT%server"
set "LOG_DIR=%ROOT%logs"

if /i "%~1"=="client" goto run_client

if not exist "%SERVER_DIR%\main.py" (
  echo server\main.py not found.
  echo Current folder: %ROOT%
  pause
  exit /b 1
)

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found.
  echo Please install Python or fix PATH, then run this file again.
  pause
  exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
del /q "%LOG_DIR%\server.log" "%LOG_DIR%\server.err.log" "%LOG_DIR%\pc_agent.log" "%LOG_DIR%\pc_agent.err.log" 2>nul

echo Starting ESPAgent local test in background mode...
echo Logs folder: %LOG_DIR%
echo.

echo Stopping old ESPAgent Python processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|py' -and $_.CommandLine -match 'D:/espagent/server/(main|pc_agent|test_client)\.py|D:\\espagent\\server\\(main|pc_agent|test_client)\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo.

echo Starting server in background...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$server='%SERVER_DIR%'; $logs='%LOG_DIR%'; Start-Process -FilePath 'py' -ArgumentList '-u',(Join-Path $server 'main.py') -WorkingDirectory $server -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'server.log') -RedirectStandardError (Join-Path $logs 'server.err.log')"

echo Waiting for server health check...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ok=$false; for($i=0; $i -lt 30; $i++){ try { Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 1 | Out-Null; $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if($ok){ exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo.
  echo Server did not start within 30 seconds.
  echo Check logs:
  echo   %LOG_DIR%\server.log
  echo   %LOG_DIR%\server.err.log
  echo.
  if exist "%LOG_DIR%\server.err.log" type "%LOG_DIR%\server.err.log"
  pause
  exit /b 1
)

echo Server is ready.
echo.

echo Starting PC Agent in background...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$server='%SERVER_DIR%'; $logs='%LOG_DIR%'; Start-Process -FilePath 'py' -ArgumentList '-u',(Join-Path $server 'pc_agent.py') -WorkingDirectory $server -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'pc_agent.log') -RedirectStandardError (Join-Path $logs 'pc_agent.err.log')"

timeout /t 1 /nobreak >nul

echo Opening test client window...
start "ESPAgent Test Client" cmd /k call "%~f0" client

echo.
echo Started.
echo Use the "ESPAgent Test Client" window to type messages.
echo Server and PC Agent are running in background.
echo To stop them, close this window and run stop_test.bat if available, or use Task Manager.
echo Logs:
echo   %LOG_DIR%\server.log
echo   %LOG_DIR%\pc_agent.log
pause
exit /b 0

:run_client
title ESPAgent Test Client
cd /d "%SERVER_DIR%"
echo [ESPAgent Test Client] %CD%
echo Running: py test_client.py
echo.
py "%SERVER_DIR%\test_client.py"
echo.
echo Test Client exited.
pause
exit /b %errorlevel%
