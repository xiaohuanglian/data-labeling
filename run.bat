@echo off
cd /d "%~dp0"
set PORT=8780
py -3 -m pip install -r requirements.txt -q
py -3 app.py
if errorlevel 1 (
  python -m pip install -r requirements.txt -q
  python app.py
)
