@echo off
chcp 65001 >nul 2>&1
title 智能期货 204 代理提示

echo 智能期货不再运行在 204，204 仅反向代理工作站 app-win-01。
echo 请查看工作站服务 pxy-futures 的日志，避免误查已停用的 pxyfutures.service。
echo.
pause
exit /b 0
