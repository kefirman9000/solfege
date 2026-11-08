@echo off
title Solfege
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup: creating a private Python environment...
  %PY% -m venv .venv || goto :fail
)

if not exist ".venv\installed.txt" (
  echo Installing what Solfege needs. This happens once and takes a few minutes...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  echo done> ".venv\installed.txt"
)

start "" ".venv\Scripts\pythonw.exe" music_sorter.py
exit /b 0

:fail
echo.
echo Setup did not finish. Check your internet connection and run this file again.
pause
exit /b 1
