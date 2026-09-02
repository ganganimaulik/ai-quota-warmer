@echo off
REM Shared helper: resolves a usable Python interpreter into %PYEXE%.
REM Every other .bat does "call _env.bat" first, so a missing or renamed Python
REM gives a readable message instead of "'python' is not recognized".
REM No setlocal here - PYEXE must survive back into the calling script.

set "PYEXE="

where python >nul 2>&1
if not errorlevel 1 (
  REM The Microsoft Store stub resolves but refuses to run; verify it works.
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYEXE=py -3"
  )
)

if not defined PYEXE (
  echo.
  echo  [X] No working Python interpreter was found.
  echo.
  echo      Install Python 3.9 or newer from https://www.python.org/downloads/
  echo      and tick "Add python.exe to PATH" during setup.
  echo.
  exit /b 1
)

exit /b 0
