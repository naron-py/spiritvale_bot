@echo off
setlocal
cd /d "%~dp0"
title SpiritVale Multi-Circle Area Editor

echo SpiritVale multi-circle area editor
echo.
set /p "AREA_NAME=Area name: "
if not defined AREA_NAME (
    echo Area name is required.
    pause
    exit /b 1
)
set /p "AREA_RADIUS=Starting radius [50]: "
if not defined AREA_RADIUS set "AREA_RADIUS=50"

echo.
echo Keep SpiritVale open with your character in the world.
echo The first memory scan can take around 15 seconds.
echo.
python minimap_bot.py --place-circles "%AREA_NAME%" "%AREA_RADIUS%"
if errorlevel 1 echo Editor exited with an error.
echo.
pause
