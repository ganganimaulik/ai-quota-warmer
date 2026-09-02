@echo off
title Claude Code One-Time Login
cd /d "%~dp0"
echo Opening Claude Code login so you can authenticate in your browser.
echo Once the login completes, close this window.
echo.
where claude >nul 2>&1 && (claude /login) || (npx -y @anthropic-ai/claude-code /login)
echo.
pause
