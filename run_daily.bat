@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" main.py run >> run_daily.log 2>&1
echo [%date% %time%] exit=%errorlevel% >> run_daily.log
