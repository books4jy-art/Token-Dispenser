#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo ".env 파일을 만들었어요. 봇 토큰을 넣고 다시 실행해 주세요."; exit 1; }
python3 bot.py
