@echo off
"C:\Users\Fade\Desktop\spiritvale-bot-main-memory\spiritvale-bot\.venv\Scripts\python.exe" "C:\Users\Fade\Desktop\spiritvale-bot-main-memory\spiritvale-bot\tools\spiritvale_run_in_background_patch.py" --restore "D:\Games\Steam\steamapps\common\SpiritVale\SpiritVale_Data\globalgamemanagers.pre-run-in-background-20260821-131942.bak"
set "rc=%ERRORLEVEL%"
echo.
if not "%rc%"=="0" echo Restore failed. Nothing was overwritten unless verification passed.
pause
exit /b %rc%
