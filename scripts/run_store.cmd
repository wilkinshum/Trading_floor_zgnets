@echo off
REM Start Agent Lightning store with OTLP enabled (venv)
"%~dp0..\.venv\Scripts\agl.exe" store --port 45993 --log-level INFO
