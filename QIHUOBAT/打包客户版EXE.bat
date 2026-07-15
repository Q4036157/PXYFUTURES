@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0.."

echo.
echo Building PXYFUTURES customer EXE...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\packaging\build_windows.ps1"
if errorlevel 1 goto failed

echo.
echo Build completed. Opening release folder...
start "" "%CD%\release"
pause
exit /b 0

:failed
echo.
echo Build failed. Review the error messages above.
pause
exit /b 1
