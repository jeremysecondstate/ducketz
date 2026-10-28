@echo off
setlocal DisableDelayedExpansion
echo 15-minute System 1A - data collection and preparation only.
pushd "%~dp0"
if errorlevel 1 exit /b 2
if not exist ".venv\Scripts\python.exe" (
  echo Ducketz Python environment is missing.
  popd
  exit /b 2
)
".venv\Scripts\python.exe" -I -S -B "tools\intraday_data_host.py" --repository "%CD%" --datastore "C:\DATASTORE" %*
set "DUCKETZ_DATA_EXIT=%ERRORLEVEL%"
popd
exit /b %DUCKETZ_DATA_EXIT%
