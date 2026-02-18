@echo off
:: Portal Service — runs uvicorn with auto-restart
:: Designed to be called by Windows Task Scheduler

cd /d C:\Users\moltbot\.openclaw\workspace\Trading_floor_zgnets\portal

:loop
echo [%date% %time%] Starting portal...
C:\Users\moltbot\.openclaw\workspace\Trading_floor_zgnets\.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8000
echo [%date% %time%] Portal exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
