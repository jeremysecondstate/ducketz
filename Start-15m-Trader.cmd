@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ducketz Python environment is missing.
  popd
  exit /b 2
)
".venv\Scripts\python.exe" -B -m ml.intraday.manual_launcher --live %*
set "DUCKETZ_15M_EXIT=%ERRORLEVEL%"
popd
exit /b %DUCKETZ_15M_EXIT%
