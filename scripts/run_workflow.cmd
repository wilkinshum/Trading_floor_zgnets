@echo off
setlocal
pushd %~dp0..
set PYTHONPATH=%CD%\src
"%CD%\.venv\Scripts\python.exe" -m trading_floor.run --config "%CD%\configs\workflow.yaml"
popd
