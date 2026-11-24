@echo off
REM Thapar Smart Parking backend - Windows one-click start
cd /d %~dp0
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
  call .venv\Scripts\activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)
python start.py
pause
