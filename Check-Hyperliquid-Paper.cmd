@echo off
setlocal
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -B -m ml.hyperliquid_paper_loop --status
set "paperExitCode=%errorlevel%"
popd
exit /b %paperExitCode%
