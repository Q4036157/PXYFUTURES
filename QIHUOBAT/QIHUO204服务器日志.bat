@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title QIHUO 204 Server Logs

set "SERVER=root@43.167.9.204"
set "SERVICE=pxyfutures.service"

where ssh.exe >nul 2>&1
if errorlevel 1 goto missing_ssh

echo ============================================================
echo   QIHUO 204 live server logs
echo   Service: %SERVICE%
echo   Press Ctrl+C to stop
echo ============================================================
echo.

ssh.exe -tt -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 %SERVER% "journalctl -u %SERVICE% -n 100 -f --no-pager"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Log connection ended. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%

:missing_ssh
echo.
echo ssh.exe was not found. Enable the Windows OpenSSH client first.
pause
exit /b 1
