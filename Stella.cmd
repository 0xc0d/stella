@echo off
setlocal EnableExtensions
title STELLA - News Reader

REM Use UTF-8 so titles in German/Turkish/Cyrillic render correctly in cmd.exe
chcp 65001 >nul 2>&1

REM Tell Python to write stdout/stderr in UTF-8 too (Python 3.7+)
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM Run from the folder this .cmd lives in (so it finds posts_*.csv)
cd /d "%~dp0"

REM If we already re-launched ourselves into Windows Terminal, skip the wt
REM branch — otherwise the inner window re-detects wt and relaunches forever.
if /I "%~1"=="--inner" (
    shift
    goto :PICK_PYTHON
)

REM Prefer Windows Terminal (wt.exe) — it handles unicode + can launch maximized
where wt >nul 2>&1
if %ERRORLEVEL% EQU 0 goto :USE_WT

REM Otherwise resize this cmd window so it feels close to fullscreen
mode con cols=180 lines=50 >nul 2>&1
goto :PICK_PYTHON

:USE_WT
REM Re-launch ourselves inside Windows Terminal, maximized, then exit this shell
start "" wt.exe --maximized cmd /c ""%~f0" --inner"
exit /b 0

:PICK_PYTHON
REM Did we re-enter from the wt branch? strip the marker so we don't loop.
if /I "%~1"=="--inner" shift

REM Try the py launcher first (default on python.org installer), then python.
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
