@echo off
chcp 65001 >nul 2>&1
title Gangge Code - Desktop GUI

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)

:: Check PyQt6
python -c "import PyQt6" >nul 2>&1
if errorlevel 1 (
    echo PyQt6 not found. Installing...
    pip install -e ".[gui]"
    if errorlevel 1 (
        echo [ERROR] Failed to install PyQt6. Run install.bat first.
        pause
        exit /b 1
    )
)

:: Load .env if exists
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if not "%%a"=="" if not "%%a:~0,1%"=="#" (
            set "%%a=%%b"
        )
    )
)

:: Launch GUI
echo Starting Gangge Code Desktop...
python desktop\app.py

if errorlevel 1 (
    echo.
    echo [ERROR] GUI failed to start. Check the error above.
    pause
)
