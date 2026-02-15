@echo off
:: Portal Service — runs uvicorn with auto-restart
:: Designed to be called by Windows Task Scheduler

set PYTHONPATH=C:\Users\moltbot\.openclaw\workspace\Trading_floor_zgnets\src
cd /d C:\Users\moltbot\.openclaw\workspace

:loop
echo [%date% %time%] Starting portal...
C:\Python314\Scripts\uvicorn.exe Trading_floor_zgnets.portal.app.main:app --host 0.0.0.0 --port 8000
echo [%date% %time%] Portal exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
