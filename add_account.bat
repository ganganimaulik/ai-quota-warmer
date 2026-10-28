@echo off
title AI Quota Warmer - Add Account
cd /d "%~dp0"
call "%~dp0_env.bat" || (pause & exit /b 1)
echo Adds another Claude Code, Codex or Gemini login to warm. Each account gets its
echo own folder (%%USERPROFILE%%\.claude-NAME, .codex-NAME or .gemini-NAME) and its
echo own sign-in - sign in with the account you want when it asks.
echo For Gemini, leave the name empty to add the Antigravity CLI's own login instead.
echo.
set "AQW_TOOL="
set "AQW_NAME="
set /p "AQW_TOOL=Tool (claude, codex or gemini): "
set /p "AQW_NAME=Account name (e.g. work; empty for gemini's own login): "
if not defined AQW_TOOL (echo  [!] No tool given. & pause & exit /b 1)
echo.
if defined AQW_NAME (
  %PYEXE% quota_warmer.py --add-account "%AQW_TOOL%" "%AQW_NAME%"
) else (
  %PYEXE% quota_warmer.py --add-account "%AQW_TOOL%"
)
if errorlevel 1 (echo. & echo  [!] Command exited with an error, code %errorlevel%.)
echo.
pause
