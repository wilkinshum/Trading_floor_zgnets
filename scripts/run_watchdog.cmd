@echo off
setlocal
pushd %~dp0..
set PYTHONPATH=%CD%\src
"%CD%\.venv\Scripts\python.exe" "%CD%\scripts\watchdog.py"
popd
