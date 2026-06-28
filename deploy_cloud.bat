@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "REMOTE=root@39.106.190.124"
set "REMOTE_DIR=/opt/espagent"

cd /d "%ROOT%"

echo ============================================
echo   XiaoAn cloud deploy
echo   Local : %ROOT%
echo   Remote: %REMOTE%:%REMOTE_DIR%
echo ============================================
echo.

echo [1/3] Commit and push local changes...
call "%ROOT%\push.bat" --run --nopause
if errorlevel 1 (
  echo.
  echo Local git push failed. Stop.
  pause
  exit /b 1
)
echo.

echo [2/3] Pull and restart cloud service...
ssh %REMOTE% "cd %REMOTE_DIR% && git pull && cd %REMOTE_DIR%/server && grep -n 'app_clients\|has_sensor_data\|/ws/app' main.py && systemctl restart esp32server"
if errorlevel 1 (
  echo.
  echo Remote deploy failed. Check SSH password, git pull, or systemctl permission.
  pause
  exit /b 1
)
echo.

echo [3/3] Cloud health check...
ssh %REMOTE% "systemctl status esp32server --no-pager -l | head -40; echo; curl -s http://127.0.0.1:8000/health; echo"
if errorlevel 1 (
  echo.
  echo Health check failed.
  pause
  exit /b 1
)

echo.
echo ============================================
echo   Done. Open:
echo   http://39.106.190.124:8000/health
echo ============================================
pause
