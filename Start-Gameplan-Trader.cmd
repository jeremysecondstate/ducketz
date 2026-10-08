@echo off
rem Manual activation: one launch now, automatic trading at the next supported 04:00 Pacific opening.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0docs\datafetch-ml\start_stock_session.ps1" -WaitForOpen -SizingPolicy gameplan-direction-current-market-v1 -ActivateForManualStart
set "traderExitCode=%errorlevel%"
if not "%traderExitCode%"=="0" (
    echo Trader startup was blocked or the worker stopped with exit code %traderExitCode%.
    echo Review the reason above. Pressing a key closes this launcher; it does not stop a running worker.
    pause
)
exit /b %traderExitCode%
