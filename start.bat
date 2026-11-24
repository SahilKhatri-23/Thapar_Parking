@echo off
REM Thapar Smart Parking backend - Windows one-click start
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment with Python 3.13...
  py -3.13 -m venv .venv 2>nul || C:\Python313\python.exe -m venv .venv 2>nul || python -m venv .venv
  call .venv\Scripts\activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)
"%~dp0.venv\Scripts\python.exe" start.py
pause
