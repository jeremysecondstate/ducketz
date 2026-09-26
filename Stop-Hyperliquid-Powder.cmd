@echo off
rem Request graceful stop. Does not flatten positions or cancel manual orders.
setlocal
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m ml.hyperliquid_powder_runtime --stop
set "powderExitCode=%errorlevel%"
popd
exit /b %powderExitCode%
