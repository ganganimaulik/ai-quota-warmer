@echo off
title AI Quota Warmer - Web Dashboard
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
echo Starting AI Quota Warmer Dashboard on http://127.0.0.1:5055 ...
echo Keep this window open; closing it stops the dashboard.
echo.
%PYEXE% app_ui.py
echo.
pause
