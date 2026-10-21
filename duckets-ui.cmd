@echo off
setlocal
title Ducketz UI
echo Opening Ducketz UI.
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
".venv\Scripts\python.exe" -B -m app.main ui
set "DUCKETZ_UI_EXIT=%ERRORLEVEL%"
popd
if not "%DUCKETZ_UI_EXIT%"=="0" (
  echo Ducketz UI stopped with exit code %DUCKETZ_UI_EXIT%.
  echo Review the reason above before opening it again.
  pause
)
exit /b %DUCKETZ_UI_EXIT%
