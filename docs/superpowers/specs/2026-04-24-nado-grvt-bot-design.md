# NADO×GRVT 델타뉴트럴 양빵봇 설계서

**작성일**: 2026-04-24
**프로젝트 경로**: `D:\Codes\nado_grvt_bot\`
**참조**: 기존 `D:\Codes\delta_neutral_bot\` (StandX×Hibachi) 아키텍처 기반

---

## 1. 목적 및 우선순위

| 순위 | 목적 | 설명 |
|------|------|------|
| 1 | NADO 포인트 | 주간 95만 pt 배분, 페어 부스트 추적하여 극대화 |
| 2 | GRVT Earn on Equity | 매 4주 사이클 $300K 볼륨 + 5 trades → Base 3.5% + 레퍼럴 1% + 볼륨 +2% = 목표 6.5% APY (최대 11%) |
| 3 | GRVT 포인트 | TGE 예정, 28% 에어드롭 |
| 4 | 펀딩비 수익 | 양쪽 펀딩레이트 차이 수취 |
| 5 | 스프레드 차익 | 델타순합(스프레드 MTM) 이상 증가 시 차익 실현 |

**최소 동작 조건**: 매 GRVT 4주 사이클마다 볼륨 $300K + 5 trades 달성하여 Earn APY 유지.

---

## 2. 거래소 비교

| 항목 | NADO | GRVT |
|------|------|------|
| 체인 | Ink L2 (Kraken) | ZKsync Validium L2 |
| 정산통화 | USDT0 | USDT |
| BTC 심볼 | `product_id` (숫자) | `BTC_USDT_Perp` |
| Python SDK | `nado-protocol` | `grvt-pysdk` (CCXT 호환) |
| 인증 | EIP-712 서명 (API key 없음) | API Key + EIP-712 서명 |
| 펀딩 주기 | 1시간 | 8시간 |
| Maker 수수료 | 1bp (0.01%) | -0.01bp (리베이트) |
| Taker 수수료 | 3.5bp (0.035%) | 4.5bp (0.045%) |
| 최대 레버리지 | 20x | 50x |
| 가격 조회 | REST 폴링 (Gateway query) | WebSocket + REST fallback |

---

## 3. 핵심 파라미터

| 파라미터 | 값 | 근거 |
|---------|-----|------|
| 레버리지 | 5x | 청산거리 15~19%, 5s 변동 대비 안전배수 7.5x+ |
| 자본 | 각 $5,000 (총 $10K) | 볼륨 효율 + Earn 후 GRVT 증액 예정 |
| 포지션 크기 | $25,000 (5x) | 사이클당 $50K 볼륨 발생 |
| 폴링 간격 | 3초 | NADO REST + GRVT WS, 5x 안전 최적점 |
| 볼륨 목표 | $300K/사이클 | 6사이클로 달성, +2% APY |
| 페어 전략 | 동적 부스트 추적 | BTC 기본, 부스트 페어로 자동 전환 |

---

## 4. 아키텍처

### 디렉토리 구조

```
D:\Codes\nado_grvt_bot\
├── grvtnado.py              # 상태머신 (IDLE→ANALYZE→ENTER→HOLD→EXIT→COOLDOWN)
├── config.py                # 환경변수 기반 설정
├── models.py                # Position, Cycle, BotState, EarnState
├── strategy.py              # 진입/퇴출 판단, 펀딩레이트 비교, 모드 전환
├── pair_manager.py          # 페어 부스트 감시, 공통 페어 탐색, 최적 페어 선정
├── monitor.py               # 마진 모니터링, Circuit Breaker, 괴리 감시
├── telegram_ui.py           # 텔레그램 UI
├── run_bot.py               # Watchdog 엔트리포인트
├── exchanges/
│   ├── base_client.py       # 공통 인터페이스 (ABC)
│   ├── nado_client.py       # nado-protocol SDK 래퍼
│   └── grvt_client.py       # grvt-pysdk 래퍼
├── logs/
│   ├── bot_state.json
│   ├── cycles.jsonl
│   ├── spread_history.jsonl
│   ├── funding_history.jsonl
│   └── volume_history.jsonl
├── .env.example
└── requirements.txt
```

### 접근법: 단일 상태머신 + PairManager

```
PairManager (페어 부스트 감시, 공통 페어 탐색)
    ↓ 최적 페어 선정
DeltaNeutralBot (상태머신)
    ├── NadoClient (REST 폴링)
    └── GrvtClient (WebSocket + REST)
```

- 한 번에 하나의 페어만 운용
- 사이클 종료 시 PairManager가 다음 최적 페어 추천
- Termux 경량 구동에 최적

---

## 5. 상태머신

### 상태 흐름

```
IDLE → ANALYZE → ENTER → HOLD → EXIT → COOLDOWN → IDLE
            ↑                           |
            └─── 페어 전환 시 ──────────┘
```

### 상태별 동작

| 상태 | 동작 | 비고 |
|------|------|------|
| IDLE | PairManager에서 최적 페어 조회 | 페어 전환 포함 |
| ANALYZE | 양쪽 펀딩레이트 비교 → 방향 결정 | 양쪽 음수면 진입 보류 |
| ENTER | 청크 분할 진입 (5청크, 30초 간격) | Maker 주문, 미체결 시 재조정 |
| HOLD | 3초 폴링, 펀딩 수집, 스프레드 감시 | 메인 모니터링 루프 |
| EXIT | 양쪽 동시 청산 (5청크) | asyncio.gather |
| COOLDOWN | 모드에 따라 대기 | HOLD:3h, VOLUME:30s, URGENT:10s |

---

## 6. 거래소 클라이언트

### 공통 인터페이스 (base_client.py)

```python
class BaseExchangeClient(ABC):
    async def get_balance() -> float
    async def get_positions(symbol) -> list[Position]
    async def get_mark_price(symbol) -> float
    async def get_funding_rate(symbol) -> float
    async def place_limit_order(symbol, side, size, price) -> OrderResult
    async def close_position(symbol, slippage) -> bool
    async def cancel_all_orders(symbol) -> bool
    async def get_available_pairs() -> list[str]
    async def set_leverage(symbol, leverage) -> bool
```

### NADO 클라이언트

- SDK: `nado-protocol` (`pip install nado-protocol`)
- 인증: Private key → EIP-712 서명
- 심볼: `product_id` (숫자) — 시작 시 전체 상품 조회하여 매핑 테이블 구축
- 펀딩: 1시간 주기
- 가격: REST 폴링 (Gateway query)
- 가격 포맷: `priceX18` (가격 × 10^18) — SDK `to_x18()` 활용

### GRVT 클라이언트

- SDK: `grvt-pysdk` (CCXT 호환 인터페이스, `GrvtCcxtWS`)
- 인증: API Key 로그인 → 세션 쿠키 + EIP-712 서명
- 심볼: `BTC_USDT_Perp` 등
- 펀딩: 8시간 주기
- 가격: WebSocket (`ticker.d` 스트림) + REST fallback
- Maker 리베이트: -0.01bp → 반드시 Limit(PostOnly) 주문

### 펀딩레이트 정규화

8시간 기준 통일: NADO 1h rate × 8, GRVT 8h rate × 1

### 수수료 전략

양쪽 모두 Maker(Limit) 주문. 30초 내 미체결 시 가격 재조정 후 재시도.

---

## 7. PairManager — 동적 페어 부스트 추적

### 공통 페어 탐색

시작 시 양쪽 `get_available_pairs()` 교집합 산출. 1일 1회 갱신.

### 부스트 정보 수집 (우선순위)

1. API 엔드포인트 (있을 경우)
2. `.env` 수동 입력: `BOOST_PAIRS=BTC:4x,ETH:3x`
3. 텔레그램 명령어: `/setboost BTC 4x`

### 최적 페어 스코어링

```python
score = (boost_nado + boost_grvt) * 3.0       # 부스트 최대 가중치
      + funding_spread * 1000                   # 펀딩 차이 보상
      + log(liquidity) * 0.5                    # 유동성 최소 보장
```

- 유동성이 포지션 크기의 3배 미만이면 해당 페어 제외
- HOLD 중 페어 전환하지 않음 — 항상 정상 EXIT 후 전환

---

## 8. Monitor — Circuit Breaker & 안전장치

### 3초 폴링 사이클

매 3초: 양쪽 mark price 조회 → PnL 계산 → 스프레드 MTM → Circuit Breaker → 마진 체크

### Circuit Breaker

| 트리거 | 조건 | 동작 |
|--------|------|------|
| API 무응답 | 한쪽 5회 연속 (~15초) | 양쪽 즉시 시장가 청산 |
| Mark price 괴리 | > 3% | 텔레그램 경고 |
| Mark price 괴리 | > 5% | 양쪽 즉시 청산 |
| 마진 위험 | 한쪽 < 10% | 양쪽 즉시 청산 |
| 마진 경고 | 한쪽 < 15% | 텔레그램 경고 (30분 쿨다운) |
| 펀딩 역전 | 합산 음수 3회 연속 | 다음 EXIT 시 방향 전환 |

### 긴급 청산 순서

1. 양쪽 미체결 주문 전부 취소
2. NADO 시장가 청산 (slippage 1%) + GRVT 시장가 청산 — 동시 (asyncio.gather)
3. 30초 후 잔여 포지션 확인 → 재시도 (최대 3회)
4. 텔레그램: `[🚨 EMERGENCY EXIT] 사유: {reason}`

### 스프레드 MTM EXIT 기준

| 모드 | 이익 EXIT | 손절 EXIT |
|------|----------|----------|
| HOLD | ≥ $50 | < -$30 |
| VOLUME | ≥ 수수료×2 (~$10) | < -$30 |
| VOLUME_URGENT | ≥ 수수료×1.2 (~$6) | < -$30 |

---

## 9. GRVT Earn 관리 & 하이브리드 모드

### GRVT Earn 핵심 파라미터

| 항목 | 값 |
|------|-----|
| 사이클 | 4주 (28일), 화요일 UTC 00:00 시작 |
| Base APY | 3.5% (5 trades 필요) |
| 레퍼럴 | +1% (이미 충족) |
| 볼륨 부스트 | $300K → +2% (목표) |
| 이자 지급 | 매주 화요일, 4시간 복리 |
| 이자 한도 | Equity $100K |
| 사이클 리셋 | 볼륨, 거래수 모두 0으로 리셋 |

### EarnManager

```python
class EarnManager:
    cycle_start: datetime
    cycle_end: datetime
    target_volume: float        # $300,000
    grvt_volume: float          # 현재 사이클 누적
    grvt_trades: int            # 현재 사이클 거래 횟수
```

사이클 경계 자동 감지 → 리셋 → 텔레그램 알림 → VOLUME 모드 전환.

### 모드 자동 전환

```
사이클 시작
  ├── 5 trades 미달? → VOLUME_URGENT (즉시 체결)
  ├── 볼륨 달성? → HOLD (안정 운용)
  ├── 여유 있음 → VOLUME (MIN_HOLD 2h, cooldown 30s)
  └── 촉박함 → VOLUME_URGENT (MIN_HOLD 30m, cooldown 10s)
```

| 모드 | MIN_HOLD | COOLDOWN | 용도 |
|------|----------|----------|------|
| HOLD | 24시간 | 3시간 | 볼륨 달성 후 펀딩비 수익 |
| VOLUME | 2시간 | 30초 | 볼륨 채우기 (여유) |
| VOLUME_URGENT | 30분 | 10초 | 볼륨 채우기 (촉박) |

### 볼륨 목표: $300K 고정

- 자연 달성 시 $750K 넘으면 +3% 자동 적용 (별도 로직 불필요)
- GRVT equity $50K+ 시 상위 티어 적극 추격 고려

---

## 10. 수익 최적화

### 수익 원천

1. **펀딩비 차익**: 양쪽 레이트 차이 수취 (주 수익원)
2. **스프레드 MTM**: 가격 괴리 발생 시 차익 실현 (기회적)
3. **GRVT Maker 리베이트**: -0.01bp 수취 (누적)

### 핵심 규칙

- **펀딩 합산 음수 방향 진입 금지**: 양쪽 다 음수면 IDLE 유지
- VOLUME 모드에서도 준수, 단 2시간+ 대기 & 촉박하면 미세 음수 허용

### 진입 타이밍 최적화

스프레드가 내 포지션에 유리한 방향일 때 진입. VOLUME_URGENT는 30분 이상 기다리지 않음.

### 적자 자동 대응

최근 5사이클 평균 순수익 < -$3이면 텔레그램 경고 + HOLD 모드 전환.

---

## 11. 텔레그램 UI

### 버튼 구성

```
📊 Status | 📋 History | 💰 Earn
📈 Funding | 🔄 Rebalance | ⏹ Stop
🎯 SetBoost
```

### 자동 알림

| 이벤트 | 알림 |
|--------|------|
| 사이클 진입 | `[ENTER] BTC \| NADO LONG / GRVT SHORT \| $25K` |
| 사이클 퇴출 | `[EXIT] 사유 \| PnL \| 볼륨` |
| 페어 전환 | `[PAIR SWITCH] BTC → ETH (부스트 3x)` |
| Circuit Breaker | `[🚨 EMERGENCY] 사유` |
| 모드 전환 | `[MODE] VOLUME → HOLD (볼륨 달성!)` |
| 사이클 리셋 | `[🔄 NEW CYCLE] 날짜` |
| 일일 리포트 | 09:00 KST — 잔고, 펀딩, 볼륨, 순수익 |

### SetBoost 커맨드

```
/setboost BTC 4x ETH 3x   → 부스트 설정
/setboost clear            → 초기화 (모두 1x)
```

---

## 12. 크래시 복구 & Termux 운영

### 상태 영속화

`logs/bot_state.json`에 원자적 저장 (tmpfile + os.replace). 상태머신 변경, 포지션 변경, 볼륨 갱신 시마다.

### 크래시 복구

1. `bot_state.json` 로드
2. 양쪽 거래소 실제 포지션 조회
3. state와 대조하여 HOLD 복원 또는 IDLE 리셋
4. Earn 데이터(볼륨/거래수) 복원, 사이클 경계 체크

### Termux 최적화

| 항목 | 값 |
|------|-----|
| 로그 | 5MB × 3 로테이션 = 15MB 상한 |
| 폴링 | 3초, 유휴 시 asyncio.sleep |
| WS | GRVT WebSocket 1개만 |
| 시작 | `python3 -u run_bot.py` |
| 백그라운드 | `termux-wake-lock` 사용 권장 |

### Watchdog

```python
MAX_CRASHES = 10       # 5분 내 최대
CRASH_WINDOW = 300     # 5분
```

---

## 13. 설정 (.env.example)

```env
# === NADO ===
NADO_PRIVATE_KEY=

# === GRVT ===
GRVT_API_KEY=
GRVT_PRIVATE_KEY=
GRVT_TRADING_ACCOUNT_ID=

# === Strategy ===
LEVERAGE=5
PAIR_DEFAULT=BTC
EARN_TARGET_VOLUME=300000
MIN_HOLD_HOURS_HOLD=24
MIN_HOLD_HOURS_VOLUME=2
MIN_HOLD_HOURS_URGENT=0.5
COOLDOWN_HOLD=10800
COOLDOWN_VOLUME=30
COOLDOWN_URGENT=10
SPREAD_EXIT_HOLD=50
SPREAD_STOPLOSS=-30
MAX_HOLD_DAYS=4

# === Monitor ===
POLL_INTERVAL=3
MARGIN_WARNING_PCT=15
MARGIN_EMERGENCY_PCT=10
CIRCUIT_BREAKER_FAILS=5
PRICE_DIVERGENCE_WARN=3
PRICE_DIVERGENCE_EMERGENCY=5

# === Telegram ===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# === Logging ===
LOG_SIZE_MB=5
LOG_COUNT=3
```
