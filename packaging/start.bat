@echo off
rem ---------------------------------------------------------------------------
rem  Start the No Man's Sky inventory sorter from a source checkout (GOAL.md
rem  P2-9). Double-click it, or run it with the same flags as
rem  `python -m nms_sorter`:
rem
rem      packaging\start.bat --help
rem      packaging\start.bat --read-only
rem
rem  It looks for `py -3`, then `python`, then `python3`, and accepts the first
rem  one that is 3.9 or newer -- the version is *asked of the interpreter*
rem  rather than parsed out of `python --version`, because a `python` on PATH
rem  that is a Store stub or a 3.8 left over from something else is the common
rem  case and the string it prints is not worth trusting.
rem
rem  Exit codes match the application: 3 no free port, 4 Python too old or
rem  absent. The window is kept open on any failure, because the whole point of
rem  this file is the person who double-clicked it and would otherwise see a
rem  black window flash.
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"

set "PY="
call :try py -3
call :try python
call :try python3
if not defined PY goto nopython

pushd "%ROOT%"
%PY% -m nms_sorter %*
set "CODE=!errorlevel!"
popd
if not "!CODE!"=="0" (
    echo.
    echo The sorter stopped with exit code !CODE!.
    echo   3 = the port was busy, and so were the ten after it
    echo   4 = this Python is older than 3.9
    echo Anything else: see %%LOCALAPPDATA%%\NMS-Sorter\logs\sorter.log
    echo.
    pause
)
exit /b !CODE!

:try
if defined PY exit /b 0
%* -c "%CHECK%" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%*"
exit /b 0

:nopython
echo.
echo Python 3.9 or newer was not found on this computer.
echo.
echo The sorter is a Python program; there is nothing to install beyond
echo Python itself, because it uses only the standard library.
echo.
echo   1. Download Python from  https://www.python.org/downloads/
echo   2. In the installer, tick "Add python.exe to PATH".
echo   3. Run this file again.
echo.
echo If you would rather not install anything, use the packaged
echo NMS-Sorter.exe from the release page instead.
echo.
pause
exit /b 4
