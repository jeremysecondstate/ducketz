@echo off
rem Real public candles and model training; simulated Paper execution only.
setlocal
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -B -m ml.hyperliquid_paper_loop
set "paperExitCode=%errorlevel%"
popd
exit /b %paperExitCode%
