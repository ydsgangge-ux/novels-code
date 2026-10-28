@echo off
chcp 65001 >nul 2>&1
title Gangge Code - 演示视频生成
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先运行 install.bat
    pause
    exit /b 1
)

echo ==============================================
echo   演示视频生成  文案 + 动画 + TTS 配音 -^> mp4
echo ==============================================
echo.
echo 直接回车 = 用内置示例分镜（不需要模型）
echo 输入主题   = 由主模型自动写分镜（需 .env 里配好 LLM_PROVIDER 与 API Key）
echo.
set "TOPIC="
set /p "TOPIC=请输入视频主题: "

echo.
echo 正在生成，请稍候（1 分钟左右的成片约需 2-4 分钟）...
echo.

if "%TOPIC%"=="" (
    python -m gangge.layer4_tools.storyboard --sample
) else (
    python -m gangge.layer4_tools.storyboard --topic "%TOPIC%"
)

echo.
echo 产物默认在 .gangge\demo_videos\ 下。
pause
