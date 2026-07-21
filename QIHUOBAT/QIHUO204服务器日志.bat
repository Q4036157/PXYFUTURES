@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title QIHUO 204 服务器日志

set "SERVER=root@43.167.9.204"
set "SERVICE=pxyfutures.service"

where ssh.exe >nul 2>&1
if errorlevel 1 goto missing_ssh

echo ============================================================
echo   QIHUO 204 服务器实时日志
echo   服务：%SERVICE%
echo   按 Ctrl+C 停止查看
echo ============================================================
echo.

ssh.exe -tt -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 %SERVER% "journalctl -u %SERVICE% -n 100 -f --no-pager"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo 日志连接已结束，退出码：%EXIT_CODE%
pause
exit /b %EXIT_CODE%

:missing_ssh
echo.
echo 未找到 ssh.exe，请先安装或启用 Windows OpenSSH 客户端。
pause
exit /b 1
