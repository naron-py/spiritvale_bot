@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo SpiritVale project virtual environment was not found.
  echo Create it and install requirements.txt plus ui_bot\requirements-ui.txt.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ui_bot %*
if errorlevel 1 pause
