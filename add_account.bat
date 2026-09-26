@echo off
title AI Quota Warmer - Add Account
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
echo Adds another Claude Code or Codex login to warm. Each account gets its own
echo folder (%%USERPROFILE%%\.claude-NAME or %%USERPROFILE%%\.codex-NAME) and its
echo own browser login - sign in with the account you want when the browser opens.
echo.
set "AQW_TOOL="
set "AQW_NAME="
set /p "AQW_TOOL=Tool (claude or codex): "
set /p "AQW_NAME=Account name (e.g. work): "
if not defined AQW_TOOL (echo  [!] No tool given. & pause & exit /b 1)
if not defined AQW_NAME (echo  [!] No name given. & pause & exit /b 1)
echo.
%PYEXE% quota_warmer.py --add-account "%AQW_TOOL%" "%AQW_NAME%"
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
