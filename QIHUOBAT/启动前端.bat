@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0..\frontend"
set "VITE_DEV_PORT=3021"
set "VITE_API_PROXY_TARGET=http://127.0.0.1:3022"

echo.
echo Starting PXYFUTURES frontend: http://127.0.0.1:3021
echo Press Ctrl+C to stop.
echo.
call npm.cmd run dev -- --host 127.0.0.1 --port 3021 --strictPort

echo.
echo Frontend exited with code %ERRORLEVEL%.
pause
exit /b %ERRORLEVEL%
