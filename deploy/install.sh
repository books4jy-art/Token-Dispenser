#!/bin/sh
# 오라클 클라우드(우분투) 서버에 봇을 설치해요. 봇 폴더(Token-Dispenser) 안에서 실행하세요:
#   sh deploy/install.sh
set -e
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$BOT_DIR"

echo "▶ 패키지 설치"
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-venv curl

echo "▶ 파이썬 가상환경 + 라이브러리 설치"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    # Cloudflare Tunnel이 같은 서버에서 접속하므로 웹훅은 서버 안에서만 열어요.
    sed -i 's/^WEBHOOK_HOST=.*/WEBHOOK_HOST=127.0.0.1/' .env
    grep -q '^WEBHOOK_HOST=' .env || echo 'WEBHOOK_HOST=127.0.0.1' >> .env
    # 웹훅 비밀번호를 무작위로 만들어 둬요.
    SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed -i "s/^WEBHOOK_SECRET=.*/WEBHOOK_SECRET=$SECRET/" .env
    chmod 600 .env
    echo "▶ .env 파일을 만들었어요 (웹훅 비밀번호 자동 생성)."
fi

echo "▶ 자동 실행 서비스 등록 (shopbot)"
sed -e "s#__USER__#$RUN_USER#g" -e "s#__DIR__#$BOT_DIR#g" deploy/shopbot.service \
    | sudo tee /etc/systemd/system/shopbot.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable shopbot > /dev/null

echo "▶ 매일 새벽 4시 데이터베이스 백업 등록 (~/shopbot-backups, 14일 보관)"
CRON="0 4 * * * $BOT_DIR/.venv/bin/python $BOT_DIR/deploy/backup.py"
( crontab -l 2>/dev/null | grep -v 'deploy/backup.py'; echo "$CRON" ) | crontab -

echo "▶ cloudflared 설치"
if ! command -v cloudflared > /dev/null; then
    ARCH="$(dpkg --print-architecture)"   # arm64(A1 서버) 또는 amd64(Micro 서버)
    curl -fsSL -o /tmp/cloudflared.deb \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb"
    sudo dpkg -i /tmp/cloudflared.deb
fi

echo ""
echo "✅ 설치 완료! 다음 단계:"
echo "  1) nano $BOT_DIR/.env  → DISCORD_TOKEN, DEPOSIT_GUILD_ID 입력"
echo "  2) sudo systemctl start shopbot"
echo "  3) journalctl -u shopbot -f   (로그 확인, Ctrl+C로 나가기)"
echo "  4) 가이드의 'Cloudflare Tunnel' 단계 진행"
