@echo off
title AI Quota Warmer - Desktop App
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
%PYEXE% gui_app.py
if errorlevel 1 (echo. & echo  [!] The desktop app exited with an error. & pause)
