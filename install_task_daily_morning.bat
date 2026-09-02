@echo off
title Install AI Quota Warmer (Daily at 8:00 AM)
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
%PYEXE% quota_warmer.py --install-task --task-mode daily --task-time 08:00
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
