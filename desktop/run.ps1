<#
.SYNOPSIS
    Gangge Code Desktop — AI 编程助手启动脚本
.DESCRIPTION
    启动 PyQt6 桌面应用，自动安装缺失依赖
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Resolve-Path "$ScriptDir\.."

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Gangge Code Desktop — AI 编程助手" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
try {
    $pyVersion = & python --version 2>&1
    Write-Host "[OK] $pyVersion" -ForegroundColor Green
} catch {
    Write-Host "[错误] 未找到 Python，请安装 Python 3.11+" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}

# Check / Install PyQt6
try {
    $null = python -c "import PyQt6"
    Write-Host "[OK] PyQt6 已安装" -ForegroundColor Green
} catch {
    Write-Host "[提示] 正在安装 PyQt6..." -ForegroundColor Yellow
    & pip install PyQt6 PyQt6-QScintilla
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] PyQt6 安装失败" -ForegroundColor Red
        Write-Host "请手动运行: pip install PyQt6 PyQt6-QScintilla" -ForegroundColor Yellow
        Read-Host "按 Enter 退出"
        exit 1
    }
}

# Check other required deps
try {
    $null = python -c "import aiohttp, openai, anthropic, python_dotenv"
} catch {
    Write-Host "[提示] 正在安装项目依赖..." -ForegroundColor Yellow
    & pip install -r "$ProjectRoot\requirements.txt" 2>$null
    # Desktop-specific deps
    & pip install aiohttp openai anthropic python-dotenv aiosqlite
}

# Launch
Write-Host ""
Write-Host "[启动] 正在启动 Gangge Code Desktop..." -ForegroundColor Green
Write-Host ""

Set-Location $ProjectRoot
python desktop/app.py

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[错误] 程序异常退出，错误码: $LASTEXITCODE" -ForegroundColor Red
    Read-Host "按 Enter 退出"
}
