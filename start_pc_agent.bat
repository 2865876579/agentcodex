@echo off
title SmartPillow PC Agent

cd /d "%~dp0server"

echo ============================================
echo   SmartPillow PC Agent
echo   Server: ws://39.106.190.124:8000/ws/pc_agent
echo ============================================
echo.

set WS_URL=ws://39.106.190.124:8000/ws/pc_agent

echo Starting...
echo Close this window to stop PC Agent.
echo.

py pc_agent.py

pause
