@echo off
rem Piksy - double-click launcher for Windows.
rem The Start Menu / Desktop shortcut points here. Safe to run directly too.

cd /d "%~dp0"
title Piksy

echo.
echo   Piksy is starting...
echo   The Piksy window will open in a moment.
echo.
echo   This black window is just the engine - you can ignore it.
echo   Closing the Piksy window stops everything.
echo.

rem Prefer the project's own virtualenv; fall back to a system Python.
rem Flat gotos on purpose: chaining "if ... && set" inside cmd parses unreliably.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if defined PY goto :gotpy

where py.exe >nul 2>&1
if not errorlevel 1 set "PY=py"
if defined PY goto :gotpy

where python.exe >nul 2>&1
if not errorlevel 1 set "PY=python"

:gotpy
if not defined PY (
    echo   [X] Python was not found on this PC.
    echo       Install it with:  winget install --id Python.Python.3.12 -e
    echo       Tick "Add python.exe to PATH", then run this again.
    echo.
    pause
    exit /b 1
)

"%PY%" run.py --app %*
set "RC=%ERRORLEVEL%"

rem On a crash the window would vanish before the error could be read - hold it open.
if not "%RC%"=="0" (
    echo.
    echo   Piksy exited with an error ^(code %RC%^).
    echo   Scroll up to see what went wrong.
    echo.
    pause
)
exit /b %RC%
