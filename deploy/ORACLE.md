# 오라클 클라우드에 봇 올리기 (24시간 무료)

순서: ① 오라클 가입 → ② 서버 만들기 → ③ 봇 설치 → ④ 무료 https 주소 만들기 (DuckDNS) → ⑤ 계좌 주인 폰 연결

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

## ④ 무료 https 주소 만들기 (DuckDNS)

계좌 주인의 폰이 봇에게 입금 알림을 보낼 **고정 https 주소**가 필요해요. 도메인을 살 필요 없이 무료로 만들 수 있어요.

### 1. DuckDNS 이름 만들기
1. https://www.duckdns.org 접속 → 구글이나 GitHub 계정으로 로그인
2. **sub domain** 칸에 원하는 이름 입력 (예: `myshop`) → **add domain**
3. 화면 위쪽의 **token**(긴 문자열)을 복사해 두세요

### 2. 오라클에서 80, 443 포트 열기
1. 오라클 콘솔 → **☰ → 네트워킹 → 가상 클라우드 네트워크 → shopbot-vcn**
2. **보안 목록(Security Lists)** → **Default Security List for shopbot-vcn**
3. **수신 규칙 추가(Add Ingress Rules)**:
   - 소스 CIDR: `0.0.0.0/0`
   - IP 프로토콜: `TCP`
   - 대상 포트 범위: `80,443`
4. **수신 규칙 추가** 버튼으로 저장

### 3. 서버에서 스크립트 실행
```sh
cd ~/Token-Dispenser && git pull
sh deploy/https.sh myshop 복사한토큰
```
`✅ 완료! https://myshop.duckdns.org 이 연결됐어요.`가 나오면 성공이에요.
(방화벽 열기, https 인증서 자동 발급, 봇 설정까지 한 번에 해요.)

## ⑤ 계좌 주인 폰 연결

디스코드에서 **`/입금 폰설정`** 을 실행하면 계좌 주인에게 보낼 안내가 나와요. 주소까지 들어 있으니
**그대로 복사해서 계좌 주인에게만** 보내 주세요. (주소 안에 비밀번호가 있어요)

계좌 주인은 안드로이드 폰에 MacroDroid(무료)를 깔고 안내대로 매크로 하나만 만들면 돼요 (약 5분).
- 설정 후 "테스트"를 누르면 로그 채널에 **📱 입금 알림 폰 연결 확인**이 올라와요.
- 그다음 1,000원 정도로 실제 충전을 한 번 해 보세요. 자동으로 충전되면 끝!
- 알림을 못 읽으면 로그 채널에 **⚠️ 읽지 못한 은행 알림**과 알림 내용이 올라와요. 그 내용을 알려 주면 읽도록 고쳐 드려요.

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
