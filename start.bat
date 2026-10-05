@echo off
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
if not exist .env (copy .env.example .env & echo .env 파일을 만들었어요. 봇 토큰을 넣고 다시 실행해 주세요. & pause & exit /b)
python bot.py
pause
