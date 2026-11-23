@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Novels Code - Install

echo ============================================
echo   Novels Code - Dependency Installer
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

:: Check pip. Some Python installs ship python.exe on PATH but not pip.exe,
:: so always go through "python -m pip" and bootstrap it if missing.
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [WARN] pip not found, trying to bootstrap with ensurepip...
    python -m ensurepip --upgrade >nul 2>&1
)
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is not available for this Python.
    echo         Try running: python -m ensurepip --upgrade
    pause
    exit /b 1
)
echo [OK] pip ready
echo.

:: Ask install mode. Office extras (docx/xlsx/pptx + pymupdf) are part of every
:: mode: office_info/office_read/office_check/office_render are core features,
:: and leaving them out only produces a "missing dependency" error later.
echo Select install mode:
echo   1. Minimal   (CLI + TUI + Office tools)
echo   2. With GUI  (CLI + TUI + Office tools + Desktop GUI)
echo   3. Full      (GUI + Office + Dev tools)
echo.
set /p MODE="Enter choice [1/2/3] (default=2): "
if "%MODE%"=="" set MODE=2

echo.
echo Installing...

if "%MODE%"=="1" (
    python -m pip install -e ".[office]"
) else if "%MODE%"=="3" (
    python -m pip install -e ".[all]"
) else (
    python -m pip install -e ".[gui,office]"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed. Check the error above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   LibreOffice (optional, for xlsx recalc / PDF render)
echo ============================================
echo.
echo office_recalc and office_render need LibreOffice (soffice).
echo Without it those two tools explain what is missing instead of failing silently.

set "SOFFICE="
where soffice >nul 2>&1 && set "SOFFICE=soffice"
if not defined SOFFICE if exist "%ProgramFiles%\LibreOffice\program\soffice.exe" set "SOFFICE=%ProgramFiles%\LibreOffice\program\soffice.exe"
if not defined SOFFICE if exist "%ProgramFiles(x86)%\LibreOffice\program\soffice.exe" set "SOFFICE=%ProgramFiles(x86)%\LibreOffice\program\soffice.exe"
if not defined SOFFICE if exist "%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe" set "SOFFICE=%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"

if defined SOFFICE (
    echo [OK] LibreOffice found: !SOFFICE!
    goto :libreoffice_done
)

echo [WARN] LibreOffice not found.
where winget >nul 2>&1
if errorlevel 1 (
    echo        winget is not available either. Install manually:
    echo        https://www.libreoffice.org/download/
    goto :libreoffice_done
)

set /p LO_INSTALL="Install LibreOffice now with winget? [y/N]: "
if /i not "!LO_INSTALL!"=="y" goto :libreoffice_done

echo.
echo Installing LibreOffice (about 350 MB download, please wait)...
winget install --id TheDocumentFoundation.LibreOffice -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo [WARN] LibreOffice install failed. Install manually:
    echo        https://www.libreoffice.org/download/
) else (
    echo [OK] LibreOffice installed. Reopen the terminal so PATH updates take effect.
)

:libreoffice_done

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
