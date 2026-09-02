@echo off
title AI Quota Warmer - Status ^& Countdown
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
%PYEXE% quota_warmer.py --status
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
