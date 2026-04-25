# NADO x GRVT 델타뉴트럴 양빵봇

NADO (Ink L2)와 GRVT (ZKsync Validium) 거래소 간 델타뉴트럴 아비트라지 봇.
양쪽 거래소에 반대 포지션을 잡아 시장 방향성 리스크를 제거하고, 펀딩비/스프레드/포인트/볼륨 인센티브를 수확한다.

## 목적

1. **NADO 포인트** 축적 (주식 페어 4x 부스트)
2. **GRVT Earn 이자** (4주 사이클, 5회 거래 + $300K 볼륨 → 6.5% APY)
3. **GRVT 포인트** (주식/원자재 3x 효율)
4. **펀딩비 수익** (NADO 1h vs GRVT 8h 금리 차이 수확)
5. **스프레드 차익** (거래소 간 가격 괴리 시 실현)

## 아키텍처

```
상태머신: IDLE → ANALYZE → ENTER → HOLD → EXIT → COOLDOWN → IDLE
                                    ↓
                              긴급청산 (Circuit Breaker / 가격괴리)
```

- **청크 진입/퇴출**: 5개 청크로 분할 주문 (슬리피지 최소화)
- **유동성 인식**: 오더북 depth의 10% 이하로 진입 제한, 얇은 시장 감지
- **자동 페어 로테이션**: 펀딩스프레드 + 유동성 + 포인트부스트 기반 최적 페어 선정
- **크래시 복구**: 원자적 상태 저장 + 재시작 시 포지션 자동 복원
- **Watchdog**: 크래시 시 자동 재시작 (5분 내 10회 초과 시 영구 종료)

## 지원 페어

| 페어    | NADO | GRVT | GRVT 포인트 배율 |
| ----- | ---- | ---- | ----------- |
| BTC   | O    | O    | 1x          |
| ETH   | O    | O    | 1x          |
| SOL   | O    | O    | 1x          |
| AAPL  | O    | O    | 3x          |
| AMZN  | O    | O    | 3x          |
| MSFT  | O    | O    | 3x          |
| META  | O    | O    | 3x          |
| GOOGL | O    | O    | 3x          |
| NVDA  | O    | O    | 3x          |
| TSLA  | O    | O    | 3x          |

## 파일 구조

```
nado_grvt_bot/
├── grvtnado.py            # 실행 진입점 (Watchdog)
├── nado_grvt_engine.py    # DeltaNeutralBot 클래스 (상태머신)
├── config.py              # 환경변수 설정 관리
├── models.py              # Position, Cycle, EarnState, BotState
├── strategy.py            # 펀딩 정규화, 방향 결정, 모드 선택
├── monitor.py             # CircuitBreaker, 마진/가격 모니터링
├── pair_manager.py        # 멀티페어 로테이션, 부스트 스코어링
├── telegram_ui.py         # 텔레그램 버튼 UI, 알림
├── exchanges/
│   ├── base_client.py     # 거래소 ABC 인터페이스
│   ├── nado_client.py     # NADO SDK 래퍼 (nado-protocol)
│   └── grvt_client.py     # GRVT CCXT 래퍼 (grvt-pysdk)
├── tests/                 # 51개 테스트
├── logs/                  # 로그 + JSONL 히스토리
├── .env.example           # 환경변수 템플릿
└── requirements.txt
```

## 빠른 시작

```bash
# 1. 클론
git clone https://github.com/smartcall1/gvrtnado.git
cd gvrtnado

# 2. 의존성 설치
pip install -r requirements.txt
pip install grvt-pysdk>=0.2.0
pip install nado-protocol>=0.1.0 --no-deps
python fix_deps.py

# 3. 환경변수 설정
cp .env.example .env
# .env 편집 — 필수 4개 키 입력

# 4. 실행
python grvtnado.py
```

## 필수 환경변수

| 변수                        | 설명                    |
| ------------------------- | --------------------- |
| `NADO_PRIVATE_KEY`        | 핫월렛 개인키 (메인 지갑 사용 금지) |
| `GRVT_API_KEY`            | GRVT API 키            |
| `GRVT_PRIVATE_KEY`        | GRVT 개인키              |
| `GRVT_TRADING_ACCOUNT_ID` | GRVT 트레이딩 계정 ID       |

나머지 파라미터는 기본값이 있어 선택 사항. 상세: `.env.example` 참조.

## 운영 모드

| 모드            | 최소 보유 | 쿨다운 | 목적            |
| ------------- | ----- | --- | ------------- |
| HOLD          | 24시간  | 3시간 | 펀딩비 수확 극대화    |
| VOLUME        | 2시간   | 30초 | Earn 볼륨 목표 달성 |
| VOLUME_URGENT | 30분   | 10초 | 볼륨 마감 임박 시    |

모드는 Earn 사이클 잔여일/볼륨에 따라 자동 전환된다.

## 텔레그램 명령어

| 버튼                 | 기능                    |
| ------------------ | --------------------- |
| 📊 Status          | 현재 상태, 스프레드 MTM, PnL  |
| 📋 History         | 최근 5 사이클 히스토리         |
| 💰 Earn            | GRVT Earn 진행률 (거래/볼륨) |
| 📈 Funding         | 현재 펀딩레이트, 누적 수익       |
| 🔄 Rebalance       | 수동 EXIT 트리거           |
| ⏹ Stop             | 포지션 청산 후 봇 종료         |
| `/setboost BTC:4x` | 포인트 부스트 수동 설정         |
| `/setboost clear`  | 부스트 초기화               |

## 안전장치

- **Circuit Breaker**: API 연속 5회 실패 시 긴급청산
- **마진 모니터링**: 15% 경고, 10% 긴급청산
- **가격 괴리 감지**: 3% 경고, 5% 긴급청산
- **손절**: 스프레드 MTM -$30 시 자동 청산
- **최대 보유일**: 4일 초과 시 강제 청산
- **Watchdog**: 크래시 자동 복구, 5분 내 10회 연속 크래시 시 영구 정지

## 테스트

```bash
python -m pytest tests/ -v
```

51개 테스트: config(7), models(9), strategy(15), monitor(9), pair_manager(6), integration(5)

## 주의사항

- **핫월렛 사용 필수**: 메인 지갑 개인키를 직접 사용하지 말 것
- **소액 테스트**: 첫 실행 시 소액으로 1사이클 확인 후 증액
- **SDK API 검증**: `nado-protocol`, `grvt-pysdk`의 실제 응답 형식이 코드 가정과 다를 수 있음
- **주식 페어 유동성**: AAPL/TSLA 등은 유동성이 낮아 자동으로 소액 진입됨


