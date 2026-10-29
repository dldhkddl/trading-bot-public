@echo off
setlocal
pushd "%~dp0."
"%LocalAppData%\Programs\Python\Python312\python.exe" main.py watch >> state\watch_scheduler.log 2>&1
set "watch_exit=%errorlevel%"
popd
exit /b %watch_exit%
