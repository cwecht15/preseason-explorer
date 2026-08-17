@echo off
REM Double-click this to start the Preseason Explorer locally.
REM Works from anywhere: it cd's to its own folder first.
setlocal

cd /d "%~dp0"
title Preseason Explorer

REM ---- pick an interpreter: the py launcher first, then plain python ----
set "PY="
py -3 -c "" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo   Python isn't on your PATH. Install it from python.org
  echo   ^(tick "Add python.exe to PATH"^) and run this again.
  echo.
  pause
  exit /b 1
)

REM ---- make sure the libraries are there ----
%PY% -c "import streamlit, pandas, altair, requests" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies, one moment...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Dependency install failed - see the errors above.
    echo.
    pause
    exit /b 1
  )
)

echo.
echo   Starting Preseason Explorer... your browser will open on its own.
echo   Leave this window open while you use the app; close it or press
echo   Ctrl+C to stop.
echo.

REM 'python -m streamlit' rather than the streamlit.exe shim, which isn't
REM always on PATH even when the package is installed.
%PY% -m streamlit run preseason_app.py

REM Only reached once Streamlit exits. Hold the window open if it crashed
REM so the traceback is readable instead of vanishing.
if errorlevel 1 (
  echo.
  echo   Streamlit exited with an error - see above.
  echo.
  pause
)
endlocal
