@echo off
setlocal
cd /d "%~dp0"

python minimap_bot.py --record

echo.
pause
