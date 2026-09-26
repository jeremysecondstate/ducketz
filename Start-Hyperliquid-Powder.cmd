@echo off
rem User activation: starts one real-money Powder session. Does not edit .env.
setlocal
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m ml.hyperliquid_powder_runtime --activate --execute
set "powderExitCode=%errorlevel%"
popd
exit /b %powderExitCode%
