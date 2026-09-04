@echo off
setlocal EnableExtensions
title PXYFUTURES workstation deployment
set "PXYOPS_DEPLOY=D:\x1\x2\PXYOPS\deploy\windows\app-win-01\Deploy-AppWin01NonAdmin.ps1"
if exist "%PXYOPS_DEPLOY%" goto deploy
echo [ERROR] Missing PXYOPS deployment engine: %PXYOPS_DEPLOY%
if /I not "%PXY_AGENT_CLI%"=="1" pause
exit /b 1
:deploy
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PXYOPS_DEPLOY%" -Project PXYFUTURES %*
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" (echo [OK] PXYFUTURES deployment completed.) else (echo [ERROR] PXYFUTURES deployment failed with exit code %EXIT_CODE%.)
if /I not "%PXY_AGENT_CLI%"=="1" pause
exit /b %EXIT_CODE%
