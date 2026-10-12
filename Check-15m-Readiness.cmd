@echo off
setlocal
rem Read-only diagnostic. This command never starts or enables a trader.
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo The Ducketz Python environment is missing. No trader was started.
    exit /b 2
)
pushd "%~dp0"
if errorlevel 1 exit /b 2
"%~dp0.venv\Scripts\python.exe" -B -m ml.intraday.readiness --repository "%~dp0." --datastore "C:\DATASTORE" --profile "%~dp0scratch\cross-pc\local-profile.json" --watchlist "%~dp0datafetching\watchlist.local.txt" %*
set "readinessExitCode=%errorlevel%"
popd
exit /b %readinessExitCode%
