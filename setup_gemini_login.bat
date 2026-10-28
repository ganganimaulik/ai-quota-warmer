@echo off
title Gemini (Antigravity CLI) One-Time Login
cd /d "%~dp0"
echo Opening the Antigravity CLI so you can sign in with your Google account.
echo It asks you to sign in when it is signed out. To switch to another Google
echo account, type /logout and then /login. Type /exit once you are signed in.
echo.
where agy >nul 2>&1
if not errorlevel 1 (
  agy
) else if exist "%LOCALAPPDATA%\agy\bin\agy.exe" (
  "%LOCALAPPDATA%\agy\bin\agy.exe"
) else (
  echo  [!] The Antigravity CLI ^(agy^) was not found. Install it first, then run this again.
)
echo.
pause
