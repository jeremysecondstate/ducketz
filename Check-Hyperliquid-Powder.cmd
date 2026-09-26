@echo off
rem Read-only readiness check. Cannot start trading or arm a future session.
setlocal
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m ml.hyperliquid_powder_runtime --check
set "powderExitCode=%errorlevel%"
popd
exit /b %powderExitCode%
