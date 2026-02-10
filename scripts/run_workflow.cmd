@echo off
set PYTHONPATH=%~dp0..\src
"%~dp0..\.venv\Scripts\python.exe" -m trading_floor.run --config "%~dp0..\configs\workflow.yaml"
