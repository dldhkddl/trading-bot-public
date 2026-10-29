@echo off
chcp 65001 >nul
title Trading Bot Dashboard
cd /d "%~dp0"
echo.
echo  Dashboard: http://localhost:8787
echo  Press Ctrl+C to stop.
echo.
start "" http://localhost:8787
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" dashboard.py
pause
