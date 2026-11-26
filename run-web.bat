@echo off
chcp 65001 >nul 2>&1
title Novels Code - Web
setlocal

set "PORT=7788"
set "HOST=127.0.0.1"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)

:: Check web dependencies
python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo Web dependencies missing. Installing...
    python -m pip install fastapi uvicorn python-multipart
    if errorlevel 1 (
        echo [ERROR] Failed to install fastapi/uvicorn. Run install.bat first.
        pause
        exit /b 1
    )
)

:: .env is loaded by the app itself (layer5_llm.registry), nothing to do here.

echo.
echo   Gangge Web  -^>  http://%HOST%:%PORT%
echo   One browser tab = one independent task.
echo   Close this window to stop the server.
echo.

:: Open the browser a few seconds after the server starts listening.
start "" /b cmd /c "timeout /t 3 >nul & start http://%HOST%:%PORT%"

python -m gangge.web.server --host %HOST% --port %PORT%

if errorlevel 1 (
    echo.
    echo [ERROR] Web server failed to start. Check the error above.
    pause
)
endlocal
