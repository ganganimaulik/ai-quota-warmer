@echo off
title Uninstall AI Quota Warmer Task
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
%PYEXE% quota_warmer.py --uninstall-task
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
