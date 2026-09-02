@echo off
title AI Quota Warmer - Disable Auto-Run on Restart
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
%PYEXE% quota_warmer.py --uninstall-startup
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
