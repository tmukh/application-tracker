@echo off
cd /d "%~dp0"
if not exist .venv ( py -3 -m venv .venv )
call .venv\Scripts\activate
pip install -q -r requirements.txt
python -m tracker
pause
