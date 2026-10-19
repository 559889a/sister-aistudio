@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem start.cmd - run aistudio-api in this console (Ctrl+C stops it).
rem Override with env vars:  set PORT=8081  /  set HOST=127.0.0.1
rem                           set AISTUDIO_BROWSER_HEADLESS=false  (headed browser)
if "%PORT%"=="" set "PORT=8080"
if "%HOST%"=="" set "HOST=0.0.0.0"

set "PYTHONPATH=%CD%\src"
set "PYTHONIOENCODING=utf-8"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY set "PY=python"

"%PY%" -c "import fastapi" >nul 2>nul
if errorlevel 1 (
    echo [start] installing dependencies from requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [start] dependency install failed
        exit /b 1
    )
)

echo [start] aistudio-api on %HOST%:%PORT%   (Ctrl+C to stop)
echo [start] panel: http://127.0.0.1:%PORT%/   api: http://%HOST%:%PORT%/v1beta/models
echo.

"%PY%" main.py server --host %HOST% --port %PORT%
