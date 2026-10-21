@echo off
REM One-tap launcher for the Smart Hardware Diagnostic Scanner.
REM Double-click this file to open the app (no console window kept open).

setlocal
cd /d "%~dp0"

REM A broken interpreter must not fail silently: pythonw has no stdout and
REM no stderr, so an import error would kill the app with no message at all.
REM Probe the interpreter first and only then prefer the windowless one.
set "PROBE="
where python >nul 2>nul && set "PROBE=python"
if not defined PROBE where py >nul 2>nul && set "PROBE=py"
if not defined PROBE goto :nopython

%PROBE% -c "import tkinter, sys; sys.exit(0)" >nul 2>nul
if errorlevel 1 (
    echo Python was found, but tkinter is missing.
    echo Reinstall Python from https://www.python.org/downloads/
    echo and enable the "Tcl/Tk" option.
    pause
    exit /b 1
)

REM Prefer pythonw/pyw (no console window); fall back to python/py if unavailable.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0shds_gui.py"
    exit /b 0
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%~dp0shds_gui.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%~dp0shds_gui.py"
    exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py "%~dp0shds_gui.py"
    exit /b 0
)

:nopython
echo Python was not found on PATH.
echo Install Python 3.11+ from https://www.python.org/downloads/ and try again.
pause
