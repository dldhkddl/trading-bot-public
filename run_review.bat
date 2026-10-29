@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" main.py review >> review.log 2>&1
echo [%date% %time%] exit=%errorlevel% >> review.log
