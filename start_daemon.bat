@echo off
title AI Quota Warmer - Adaptive Background Watcher
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
echo Watching the real 5-hour windows. Warm-up fires the moment one resets.
echo Only one watcher runs at a time - if the Startup daemon is already
echo running, this window will say so and exit.
echo.
%PYEXE% quota_warmer.py --loop
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
