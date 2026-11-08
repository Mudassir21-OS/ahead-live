@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or 3.12 from python.org, then run this file again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
set "AHEAD_ENABLE_DEMO_ACCOUNTS=1"
echo.
echo AHEAD is starting. Open http://localhost:8501 in your browser.
echo Doctor demo: doctor@ahead.demo / doctor123
echo Patient demo: user@ahead.demo / user123
echo Admin demo:   admin@ahead.demo / admin123
echo Local demo only: demo accounts never work on the live site.
echo.
python -m streamlit run server.py
pause
exit /b 0
:failed
echo Setup failed. Check the error above, then try again.
pause
exit /b 1
