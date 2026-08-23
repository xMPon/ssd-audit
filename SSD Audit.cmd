@echo off
REM ---------------------------------------------------------------------------
REM  SSD Audit - double-click this file to open the desktop interface.
REM
REM  Reads two drives and reports what differs between them. It never copies,
REM  moves or deletes anything.
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

REM Prefer pythonw so no console window sits behind the app; fall back to
REM python if pythonw is unavailable, and to the py launcher after that.
where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" pythonw -m ssdaudit gui
    goto :eof
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python -m ssdaudit gui
    goto :eof
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -m ssdaudit gui
    goto :eof
)

echo.
echo  Python was not found on this machine.
echo.
echo  Install Python 3.10 or newer from https://python.org and tick
echo  "Add Python to PATH" during setup, then run this file again.
echo.
pause
