@echo off
rem Runs sol_bot.py and restarts it if it ever stops. Close this window to stop.
cd /d "%~dp0"
title pump.fun paper bot
:loop
python sol_bot.py
echo.
echo Bot stopped - restarting in 15 seconds. Close this window to stop for good.
timeout /t 15 /nobreak >nul
goto loop
