@echo off
REM ---------------------------------------------------------------------------
REM  Builds a standalone SSD Audit.exe that runs without Python installed.
REM
REM  You do not need this to use the tool - "SSD Audit.cmd" already opens the
REM  interface on any machine with Python. Build the .exe only if you want to
REM  carry the tool on the SSD itself and run it on a machine without Python.
REM
REM  Requires an internet connection the first time, to fetch PyInstaller.
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

echo.
echo  Building a standalone SSD Audit.exe
echo  This takes a couple of minutes and needs about 100 MB of temporary space.
echo.
pause

python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :failed

python -m PyInstaller ^
    --name "SSD Audit" ^
    --onefile ^
    --windowed ^
    --clean ^
    --noconfirm ^
    --collect-submodules ssdaudit ^
    launcher.py
if errorlevel 1 goto :failed

echo.
echo  ==================================================================
echo   Done.  dist\SSD Audit.exe
echo.
echo   That single file is the whole tool. Copy it anywhere - including
echo   onto one of the SSDs - and double-click it.
echo  ==================================================================
echo.
pause
goto :eof

:failed
echo.
echo  Build failed. See the messages above.
echo  The tool still works without this: just run "SSD Audit.cmd".
echo.
pause
