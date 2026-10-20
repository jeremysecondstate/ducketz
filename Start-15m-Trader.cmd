@echo off
setlocal
title Ducketz 15-minute trader
echo Ducketz 15-minute trader - manual start.
echo To stop, press Ctrl+C. If console text is selected, press Esc first.
pushd "%~dp0"
if errorlevel 1 (
  echo Could not open the Ducketz application folder.
  pause
  exit /b 2
)
if not exist ".venv\Scripts\python.exe" (
  echo Ducketz Python environment is missing.
  popd
  pause
  exit /b 2
)
".venv\Scripts\python.exe" -B -m ml.intraday.live_session --live %*
set "DUCKETZ_15M_EXIT=%ERRORLEVEL%"
popd
if not "%DUCKETZ_15M_EXIT%"=="0" (
  echo The 15-minute trader stopped with exit code %DUCKETZ_15M_EXIT%.
  echo Review the reason above before starting it again.
  pause
)
exit /b %DUCKETZ_15M_EXIT%
