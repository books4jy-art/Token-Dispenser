# 오라클 클라우드에 봇 올리기 (24시간 무료)

순서: ① 오라클 가입 → ② 서버 만들기 → ③ 봇 설치 → ④ Cloudflare Tunnel로 https 주소 만들기 → ⑤ 휴대폰 연결

---

## ① 오라클 클라우드 가입

1. https://www.oracle.com/kr/cloud/free/ → **무료로 시작하기**
2. **홈 리전은 `South Korea Central (Seoul)` 또는 `South Korea North (Chuncheon)`** 으로 고르세요.
   ⚠️ 홈 리전은 나중에 바꿀 수 없고, 무료 서버는 홈 리전에서만 만들 수 있어요.
3. 카드 등록은 본인 확인용이에요. (무료 한도 안에서는 요금이 나가지 않아요)

### (추천) 종량제(Pay As You Go)로 업그레이드

무료 계정의 서버는 7일 동안 거의 놀고 있으면 오라클이 **회수**할 수 있어요. 봇은 대부분 쉬고 있어서 해당될 수 있어요.
**결제 → 계정 업그레이드 → Pay As You Go**로 바꾸면 회수되지 않고, 무료 한도 안에서 쓰면 여전히 0원이에요.
안심하려면 **결제 → 예산(Budgets)** 에서 1달러 예산 알림을 만들어 두세요.

## ② 서버(인스턴스) 만들기

1. 메뉴 → **컴퓨트 → 인스턴스 → 인스턴스 생성**
2. **이미지**: `Canonical Ubuntu 24.04` (또는 22.04)
3. **Shape**: `Ampere → VM.Standard.A1.Flex`, **OCPU 1개 / 메모리 6GB** (봇에는 충분해요)
   - "Out of capacity(용량 부족)"가 뜨면: 다른 가용성 도메인(AD) 선택, 시간을 두고 다시 시도,
     또는 `VM.Standard.E2.1.Micro` (AMD, 메모리 1GB — 이것도 봇에 충분해요)
4. **SSH 키 추가**: `개인 키 저장`을 눌러 키 파일(`.key`)을 꼭 내려받아 두세요. 잃어버리면 접속할 수 없어요.
5. **생성** → 상태가 `실행 중`이 되면 **공용 IP 주소**를 복사해요.

> 포트를 열 필요는 없어요. ④의 Cloudflare Tunnel은 서버에서 밖으로 연결하는 방식이라 방화벽 설정이 필요 없어요.

## ③ 봇 설치

### 서버 접속
윈도우 PowerShell / 맥 터미널에서:
```sh
ssh -i 내려받은키.key ubuntu@공용IP
```
(맥/리눅스에서 권한 오류가 나면 먼저 `chmod 600 내려받은키.key`)

### 봇 파일 올리기
저장소가 **공개**라면 서버에서:
```sh
git clone https://github.com/books4jy-art/Token-Dispenser.git
cd Token-Dispenser
```
저장소가 **비공개**라면 내 PC에서 `Token-Dispenser` 폴더를 올려요:
```sh
scp -i 내려받은키.key -r Token-Dispenser ubuntu@공용IP:~/
```
그다음 서버에서 `cd ~/Token-Dispenser`

### 설치 스크립트 실행
```sh
sh deploy/install.sh
```
파이썬 라이브러리, 자동 실행 서비스, 매일 백업, cloudflared를 한 번에 설치하고
웹훅 비밀번호(`WEBHOOK_SECRET`)도 무작위로 만들어 줘요.

### 토큰 입력 후 시작
```sh
nano .env          # DISCORD_TOKEN, DEPOSIT_GUILD_ID 입력 → Ctrl+O, Enter, Ctrl+X로 저장
sudo systemctl start shopbot
journalctl -u shopbot -f     # "로그인 완료"가 보이면 성공 (Ctrl+C로 나가기)
```
이제 서버가 재부팅되거나 봇이 오류로 꺼져도 자동으로 다시 켜져요.

## ④ Cloudflare Tunnel로 https 주소 만들기

휴대폰이 봇에게 입금 알림을 보낼 **고정 https 주소**가 필요해요. 도메인이 하나 있어야 해요.

1. 도메인이 없다면 [Cloudflare Registrar](https://dash.cloudflare.com/?to=/:account/domains/register)에서 구매 (.com 약 $10/년).
   다른 곳에서 산 도메인이라면 Cloudflare에 사이트를 추가하고 네임서버를 바꿔 주세요.
2. [Cloudflare 대시보드](https://one.dash.cloudflare.com/) → **Zero Trust → Networks → Tunnels → Create a tunnel**
3. **Cloudflared** 선택 → 이름 `shopbot` → 환경은 **Debian**
4. 화면에 나오는 `sudo cloudflared service install eyJ...` 명령을 **복사해서 서버에서 실행**
   (cloudflared는 설치 스크립트가 이미 설치했으니 설치 명령은 건너뛰고 이 줄만 실행하면 돼요)
5. **Public Hostname** 추가:
   - Subdomain: `bot` / Domain: 내 도메인
   - Service: `HTTP` / URL: `localhost:8080`
6. 저장 후 브라우저에서 `https://bot.내도메인` 에 접속해 `ok`가 보이면 성공!

## ⑤ 휴대폰 연결

서버에서 웹훅 비밀번호를 확인하세요:
```sh
grep WEBHOOK_SECRET .env
```
MacroDroid의 HTTP 요청 주소에 넣어요:
```
https://bot.내도메인/deposit?token=WEBHOOK_SECRET값
```
나머지 설정은 [README](../README.md)의 "휴대폰 설정"을 보세요.
디스코드에서 `/입금 테스트`로 은행 알림이 잘 읽히는지 확인하고, 1,000원 정도로 실제 충전을 한 번 해 보세요.

---

## 자주 쓰는 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 로그 보기 | `journalctl -u shopbot -f` |
| 재시작 (.env를 바꾼 뒤) | `sudo systemctl restart shopbot` |
| 멈추기 | `sudo systemctl stop shopbot` |
| 봇 업데이트 (git으로 받은 경우) | `git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart shopbot` |
| 백업 확인 | `ls ~/shopbot-backups` |

백업은 같은 서버 안에 있어서 서버가 사라지면 함께 사라져요. 가끔 내 PC로도 받아 두세요:
```sh
scp -i 내려받은키.key -r ubuntu@공용IP:~/shopbot-backups ./
```
