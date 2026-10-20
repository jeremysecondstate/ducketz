@echo off
setlocal
title Ducketz Gameplan trader
rem Manual start: finish account setup, then run or wait for the supported stock session.
rem Ctrl-C or closing this manual launcher stops its verified trader process.
echo Ducketz Gameplan trader - manual start.
echo To stop, press Ctrl+C. If console text is selected, press Esc first.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0docs\datafetch-ml\start_stock_session.ps1" -WaitForOpen -SizingPolicy gameplan-direction-current-market-v1 -ActivateForManualStart
set "traderExitCode=%errorlevel%"
if not "%traderExitCode%"=="0" (
    echo Trader startup was blocked or the worker stopped with exit code %traderExitCode%.
    echo Review the reason above. A pre-existing trader may still be running; use the reported worker identity before trying again.
    pause
)
exit /b %traderExitCode%
