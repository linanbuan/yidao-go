@echo off
rem =====================================================================
rem  Yidao (Go AI teaching platform) - double-click to run
rem
rem  Opens the launcher: brand window with login/register, engine status,
rem  KataGo install and LLM config; "Enter Yidao" starts the desktop client.
rem  Runs with the project venv; prints a short message if it is missing.
rem
rem  This file is intentionally pure ASCII: cmd.exe parses .bat files
rem  with the OEM code page, so any non-ASCII literal here could turn
rem  into mojibake.
rem =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem The venvs are NOT created by the launcher any more - a fresh clone has to
rem bootstrap them once. Point at the script that does exactly that, instead of
rem telling people to "run the setup step" without saying which one.
if not exist "%~dp0desktop\.venv\Scripts\pythonw.exe" (
    echo.
    echo [ERROR] desktop\.venv was not found.
    echo.
    echo   Run this once, then double-click this file again:
    echo     powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
    echo.
    pause
    exit /b 1
)
if not exist "%~dp0desktop\.venv\Lib\site-packages\PySide6" (
    echo.
    echo [ERROR] PySide6 is missing in desktop\.venv.
    echo.
    echo   Reinstall it with:
    echo     powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1 -Force
    echo.
    pause
    exit /b 1
)

start "" "%~dp0desktop\.venv\Scripts\pythonw.exe" "%~dp0launcher\gui.py"
exit /b 0