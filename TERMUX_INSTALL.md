# Termux 설치 가이드

Android Termux 환경에서 NADO x GRVT 봇을 설치하고 운영하는 방법.

## 1. Termux 기본 설정

```bash
# 패키지 업데이트
pkg update && pkg upgrade -y

# 필수 패키지
pkg install -y python git openssh rust binutils

# pip 업그레이드
pip install --upgrade pip setuptools wheel
```

> Rust는 일부 Python 패키지(pynacl 등)의 빌드에 필요하다.

## 2. 프로젝트 클론 및 설치

```bash
# 클론
git clone https://github.com/smartcall1/gvrtnado.git
cd gvrtnado

# 공통 의존성
pip install -r requirements.txt

# SDK 설치 (eth-account 버전 충돌 때문에 순서 중요)
pip install grvt-pysdk>=0.2.0
pip install nado-protocol>=0.1.0 --no-deps

# nado-protocol 호환성 패치
python fix_deps.py
```

### 빌드 에러 발생 시

```bash
# pynacl 빌드 실패 시
pkg install -y libsodium
SODIUM_INSTALL=system pip install pynacl

# aiohttp 빌드 실패 시
pkg install -y python-dev
pip install aiohttp --no-build-isolation
```

## 3. 환경변수 설정

```bash
cp .env.example .env
nano .env
```

**반드시 입력해야 하는 4개 키:**

```
NADO_PRIVATE_KEY=0x...       # 핫월렛 개인키
GRVT_API_KEY=...             # GRVT API 키
GRVT_PRIVATE_KEY=0x...       # GRVT 개인키
GRVT_TRADING_ACCOUNT_ID=...  # GRVT 트레이딩 계정 ID
```

텔레그램 알림을 쓰려면:
```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-100...
```

나머지 파라미터는 기본값 그대로 사용 가능.

## 4. 테스트

```bash
python -m pytest tests/ -v
```

51개 모두 PASSED 확인 후 진행.

## 5. 실행

### 포그라운드 실행 (테스트용)

```bash
python grvtnado.py
```

`Ctrl+C`로 종료.

### 백그라운드 실행 (운영용)

```bash
nohup python -u grvtnado.py > logs/stdout.log 2>&1 &
echo $! > .bot_pid
```

### tmux 사용 (권장)

```bash
pkg install -y tmux

# 세션 생성
tmux new -s grvtnado

# 봇 실행
python grvtnado.py

# 세션 분리: Ctrl+B → D
# 세션 복귀:
tmux attach -t grvtnado
```

## 6. 운영 명령어

### 상태 확인

```bash
# 프로세스 확인
ps aux | grep grvtnado

# 로그 실시간
tail -f logs/bot.log

# 최근 사이클 확인
tail -5 logs/cycles.jsonl | python -m json.tool
```

### 정지

```bash
# 텔레그램 ⏹ Stop 버튼 사용 (권장)
# 또는 stop 파일 생성
touch .stop_bot

# 또는 강제 종료
kill $(cat .bot_pid)
```

### 재시작

```bash
rm -f .stop_bot
nohup python -u grvtnado.py > logs/stdout.log 2>&1 &
echo $! > .bot_pid
```

## 7. Termux 절전 방지

Android가 Termux를 백그라운드에서 죽이지 않도록 설정:

```bash
# Termux wake lock 활성화
termux-wake-lock
```

추가로 Android 설정에서:
- **설정 → 앱 → Termux → 배터리 → 무제한** (제조사별 상이)
- **설정 → 배터리 → 배터리 최적화 → Termux → 최적화하지 않음**
- MIUI/Samsung: 자동 시작 관리자에서 Termux 허용

## 8. 자동 시작 (Termux:Boot)

Play Store에서 `Termux:Boot` 설치 후:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start_grvtnado.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd ~/gvrtnado
python -u grvtnado.py > logs/stdout.log 2>&1 &
EOF
chmod +x ~/.termux/boot/start_grvtnado.sh
```

폰 재부팅 시 자동으로 봇이 시작된다.

## 9. 로그 관리

봇 로그(`logs/bot.log`)는 자동 로테이션된다 (5MB x 3파일).

JSONL 히스토리 파일은 수동 관리 필요:
```bash
# spread_history.jsonl 크기 확인 (3초마다 기록, 하루 ~5-10MB)
du -sh logs/*.jsonl

# 오래된 스프레드 로그 정리 (최근 1일만 유지)
tail -28800 logs/spread_history.jsonl > logs/spread_tmp.jsonl
mv logs/spread_tmp.jsonl logs/spread_history.jsonl
```

## 10. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `ModuleNotFoundError: nado_protocol` | SDK 미설치 | `pip install nado-protocol` |
| `ModuleNotFoundError: grvt_pysdk` | SDK 미설치 | `pip install grvt-pysdk` |
| 봇이 바로 종료됨 | `.env` 키 누락 | `logs/bot.log` 확인, 필수 키 입력 |
| `EMERGENCY EXIT` 반복 | API 연결 불안정 | 네트워크 확인, VPN 시도 |
| 텔레그램 응답 없음 | 토큰/채팅ID 누락 | `.env`에 `TELEGRAM_*` 설정 |
| `pynacl` 빌드 실패 | libsodium 없음 | `pkg install libsodium` |
| Termux 백그라운드 종료 | 배터리 최적화 | `termux-wake-lock` + 배터리 설정 |
| 5분 내 10회 크래시 | 근본 에러 반복 | `logs/bot.log` 확인, 원인 해결 후 재시작 |
