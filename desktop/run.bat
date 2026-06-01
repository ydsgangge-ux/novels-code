@echo off
chcp 65001 >nul
title Gangge Code Desktop

cd /d "%~dp0.."

echo ========================================
echo   Gangge Code Desktop — AI 编程助手
echo ========================================
echo.

REM Check Python availability
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [错误] 未找到 Python，请安装 Python 3.11+
    pause
    exit /b 1
)

REM Check PyQt6
python -c "import PyQt6" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [提示] 正在安装 PyQt6...
    pip install PyQt6
    if %ERRORLEVEL% NEQ 0 (
        echo [错误] PyQt6 安装失败，请手动运行: pip install PyQt6
        pause
        exit /b 1
    )
)

echo [启动] 正在启动 Gangge Code Desktop...
echo.

python desktop/app.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [错误] 程序异常退出，错误码: %ERRORLEVEL%
    pause
)
