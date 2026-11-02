@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONPATH=src

echo 正在生成样式工作台...
python -m gangge.layer4_tools.storyboard.studio -o ".gangge\style-studio.html" --fps 30
if errorlevel 1 (
  echo.
  echo 生成失败：先确认已经装好依赖（pip install -e ".[gui]"）
  pause
  exit /b 1
)

if not exist ".gangge\style-studio.html" (
  echo 没找到生成的 style-studio.html
  pause
  exit /b 1
)

echo 正在打开浏览器...
start "" ".gangge\style-studio.html"
