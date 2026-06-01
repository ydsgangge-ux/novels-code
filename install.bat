@echo off
chcp 65001 >nul 2>&1
title Gangge Code - Install

echo ============================================
echo   Gangge Code - Dependency Installer
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11+
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% detected
echo.

:: Ask install mode
echo Select install mode:
echo   1. Minimal   (CLI + TUI only)
echo   2. With GUI  (CLI + TUI + Desktop GUI)
echo   3. Full      (GUI + Dev tools)
echo.
set /p MODE="Enter choice [1/2/3] (default=2): "
if "%MODE%"=="" set MODE=2

echo.
echo Installing...

if "%MODE%"=="1" (
    pip install -e .
) else if "%MODE%"=="3" (
    pip install -e ".[all]"
) else (
    pip install -e ".[gui]"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed. Check the error above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Installation complete!
echo ============================================
echo.
echo Next steps:
echo   1. Copy .env.example to .env
echo   2. Edit .env and add your API key
echo   3. Run:  gangge "your task"
echo       or:  run-gui.bat
echo.
pause
