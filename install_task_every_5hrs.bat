@echo off
title Install AI Quota Warmer (Just After Each 5-Hour Reset)
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
%PYEXE% quota_warmer.py --install-task --task-mode interval --interval 5
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
