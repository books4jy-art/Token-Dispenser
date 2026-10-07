#!/bin/sh
# Free https address for the deposit webhook: DuckDNS name + Caddy (automatic certificate).
# Run inside the bot folder:   sh deploy/https.sh <duckdns-name> <duckdns-token>
#   e.g.  sh deploy/https.sh myshop a1b2c3d4-....
# Before running: make the name at https://www.duckdns.org, and open TCP 80 and 443 in
# Oracle Cloud (VCN → public subnet → security list → add ingress rules).
set -e
NAME="$(echo "${1:-}" | sed 's/\.duckdns\.org$//')"
TOKEN="${2:-}"
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "$NAME" ] || [ -z "$TOKEN" ]; then
    echo "사용법: sh deploy/https.sh <DuckDNS 이름> <DuckDNS 토큰>"
    echo "  예:   sh deploy/https.sh myshop a1b2c3d4-e5f6-..."
    exit 1
fi
DOMAIN="$NAME.duckdns.org"

echo "▶ DuckDNS 주소를 이 서버로 연결 ($DOMAIN)"
RESULT="$(curl -fsS "https://www.duckdns.org/update?domains=$NAME&token=$TOKEN&ip=")" || RESULT="error"
if [ "$RESULT" != "OK" ]; then
    echo "❌ DuckDNS가 거절했어요 (이름이나 토큰을 확인하세요): $RESULT"
    exit 1
fi
# Keep the address pointing here even if the server's IP ever changes.
echo "NAME=$NAME
TOKEN=$TOKEN" | sudo tee /etc/duckdns.env > /dev/null
sudo chmod 600 /etc/duckdns.env
echo '*/5 * * * * root . /etc/duckdns.env && curl -fsS "https://www.duckdns.org/update?domains=$NAME&token=$TOKEN&ip=" > /dev/null 2>&1' \
    | sudo tee /etc/cron.d/duckdns > /dev/null

echo "▶ 서버 방화벽에서 80, 443 포트 열기"
for PORT in 80 443; do
    if ! sudo iptables -C INPUT -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT 2>/dev/null; then
        # Oracle's Ubuntu image ends INPUT with a REJECT rule: insert before it.
        POS="$(sudo iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')"
        if [ -n "$POS" ]; then
            sudo iptables -I INPUT "$POS" -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT
        else
            sudo iptables -A INPUT -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT
        fi
    fi
done
if command -v netfilter-persistent > /dev/null; then
    sudo netfilter-persistent save > /dev/null 2>&1 || true
fi

echo "▶ Caddy 설치 (https 인증서 자동 발급)"
if ! command -v caddy > /dev/null; then
    sudo apt-get update -q
    sudo apt-get install -y -q caddy
fi
# Only the deposit webhook is reachable from outside; everything else answers "ok".
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
$DOMAIN {
    handle /deposit* {
        reverse_proxy 127.0.0.1:8080
    }
    handle {
        respond "ok" 200
    }
}
EOF
sudo systemctl enable caddy > /dev/null 2>&1 || true
sudo systemctl restart caddy

echo "▶ 봇 설정에 주소 저장"
ENV="$BOT_DIR/.env"
if grep -q '^WEBHOOK_PUBLIC_URL=' "$ENV"; then
    sed -i "s#^WEBHOOK_PUBLIC_URL=.*#WEBHOOK_PUBLIC_URL=https://$DOMAIN#" "$ENV"
else
    echo "WEBHOOK_PUBLIC_URL=https://$DOMAIN" >> "$ENV"
fi
sudo systemctl restart shopbot

echo "▶ https 연결 확인 중 (최대 1분)"
i=0
while [ $i -lt 12 ]; do
    if [ "$(curl -fsS "https://$DOMAIN/" 2>/dev/null)" = "ok" ]; then
        echo ""
        echo "✅ 완료! https://$DOMAIN 이 연결됐어요."
        echo "   디스코드에서 /입금 폰설정 을 실행해서 계좌 주인에게 보낼 안내를 받으세요."
        exit 0
    fi
    i=$((i + 1))
    sleep 5
done
echo ""
echo "⚠️ 아직 https로 연결되지 않아요. 확인할 것:"
echo "   1) 오라클 콘솔에서 80, 443 포트를 열었는지 (가이드 참고)"
echo "   2) 몇 분 뒤 다시: curl https://$DOMAIN/   (ok 가 나오면 성공)"
echo "   3) 로그: sudo journalctl -u caddy -n 30 --no-pager"
