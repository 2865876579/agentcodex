@echo off
setlocal

echo Stopping ESPAgent background processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|py' -and $_.CommandLine -match 'D:/espagent/server/(main|pc_agent|test_client)\.py|D:\\espagent\\server\\(main|pc_agent|test_client)\.py' } | ForEach-Object { Write-Host ('Stopped ' + $_.ProcessId + ': ' + $_.CommandLine); Stop-Process -Id $_.ProcessId -Force }"

echo Done.
pause
