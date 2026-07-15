@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0..\backend"
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" goto missing_python

set "PYTHONPATH=%CD%;%CD%\.venv\Lib\site-packages"
if exist "%LocalAppData%\Programs\Python\Python312\Lib\site-packages\tqsdk" set "PYTHONPATH=%PYTHONPATH%;%LocalAppData%\Programs\Python\Python312\Lib\site-packages"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo Starting PXYFUTURES backend: http://127.0.0.1:3022
echo This window displays live backend logs. Press Ctrl+C to stop.
echo.
"%PYTHON%" -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 3022

echo.
echo Backend exited with code %ERRORLEVEL%.
pause
exit /b %ERRORLEVEL%

:missing_python
echo.
echo Backend virtual environment was not found:
echo %PYTHON%
echo.
pause
exit /b 1
