@echo off
setlocal EnableExtensions

REM Use UTF-8 so titles in German/Turkish/Cyrillic render correctly.
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Run from the folder this .cmd lives in (so it finds posts_*.csv).
cd /d "%~dp0"

REM Force the classic console host (conhost). Windows Terminal trails/ghosts
REM text while scrolling on some GPUs; the classic console does not. On Win11
REM the default terminal can be Windows Terminal even for cmd, so relaunch
REM ourselves once inside conhost.exe. The --inner guard stops an infinite loop.
if /I "%~1"=="--inner" goto :RUN
start "STELLA" conhost.exe cmd /c ""%~f0" --inner"
exit /b 0

:RUN
title STELLA - News Reader
REM Make the window large so the list has room.
mode con cols=180 lines=50 >nul 2>&1

REM Try the py launcher first (python.org default), then python.
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py -3 stella.py
    goto :DONE
)

where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    python stella.py
    goto :DONE
)

echo.
echo  Python 3.10+ is not on PATH.
echo  Install it from https://python.org/downloads/ and tick
echo  "Add Python to PATH" during the installer.
echo.
pause
exit /b 1

:DONE
echo.
echo  (STELLA exited - press any key to close this window)
pause >nul
