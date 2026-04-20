@echo off
title Telegram bot
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [.venv] not found. Create it first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bot.py
if errorlevel 1 (
    echo.
    echo Bot stopped with an error.
    pause
)
