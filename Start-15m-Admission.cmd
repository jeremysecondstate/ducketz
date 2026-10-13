@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ducketz Python environment is missing.
  popd
  exit /b 2
)
rem The host defaults to a noncommitting check. Serving requires explicit --serve.
".venv\Scripts\python.exe" -B -m ml.intraday.admission_host %*
set "DUCKETZ_ADMISSION_EXIT=%ERRORLEVEL%"
popd
exit /b %DUCKETZ_ADMISSION_EXIT%
