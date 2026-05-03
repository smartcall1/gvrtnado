"""
설정 관리 모듈

환경변수에서 설정을 읽어 로드하고, 모드별 파라미터, 수수료 계산 등을 제공합니다.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    """
    NADO×GRVT 델타 뉴트럴 아비트라지 봇의 설정을 관리하는 클래스.

    환경변수에서 설정값을 읽고, 기본값을 제공하며, 검증 메서드를 포함합니다.
    """

    def __init__(self):
        """환경변수에서 설정값을 읽어 초기화합니다."""

        # ===== 필수 인증 정보 =====
        self.NADO_PRIVATE_KEY = os.getenv("NADO_PRIVATE_KEY", "")
        self.GRVT_API_KEY = os.getenv("GRVT_API_KEY", "")
        self.GRVT_PRIVATE_KEY = os.getenv("GRVT_PRIVATE_KEY", "")
        self.GRVT_TRADING_ACCOUNT_ID = os.getenv("GRVT_TRADING_ACCOUNT_ID", "")

        # ===== 거래 기본 파라미터 =====
        self.LEVERAGE = int(os.getenv("LEVERAGE", "5"))
        self.PAIR_DEFAULT = os.getenv("PAIR_DEFAULT", "BTC")
        self.EARN_TARGET_VOLUME = float(os.getenv("EARN_TARGET_VOLUME", "300000"))

        # ===== HOLD 모드: 장기 보유 =====
        self.MIN_HOLD_HOURS_HOLD = float(os.getenv("MIN_HOLD_HOURS_HOLD", "24"))
        self.COOLDOWN_HOLD = int(os.getenv("COOLDOWN_HOLD", "10800"))  # 3시간 = 10800초

        # ===== VOLUME 모드: 중기 수익 추구 =====
        self.MIN_HOLD_HOURS_VOLUME = float(os.getenv("MIN_HOLD_HOURS_VOLUME", "2"))
        self.COOLDOWN_VOLUME = int(os.getenv("COOLDOWN_VOLUME", "30"))

        # ===== VOLUME_URGENT 모드: 단기 수익 추구 =====
        self.MIN_HOLD_HOURS_URGENT = float(os.getenv("MIN_HOLD_HOURS_URGENT", "0.5"))
        self.COOLDOWN_URGENT = int(os.getenv("COOLDOWN_URGENT", "10"))

        # ===== 손절/익절 설정 =====
        self.SPREAD_EXIT_HOLD = float(os.getenv("SPREAD_EXIT_HOLD", "50"))  # USD
        self.SPREAD_STOPLOSS = float(os.getenv("SPREAD_STOPLOSS", "-30"))  # USD
        self.MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "4"))
        # URGENT break-even 임계값 — 청산 슬리피지 흡수용.
        # 기본 2: 포인트 파밍 우선 전략 — 수수료 회수 즉시 청산, volume 회전 극대화
        # 0: 정확히 본전 트리거 (확정 손실 위험)
        # 3+: 보수적 (spread_exit와 거의 중복, URGENT 발동 드묾)
        self.URGENT_BREAK_EVEN_THRESHOLD = float(os.getenv("URGENT_BREAK_EVEN_THRESHOLD", "2"))
        # URGENT bypass max unfavorable spread (%)
        # 진입 시 거래소간 spread가 불리한 방향으로 이 % 이상이면 bypass 차단
        # 예: 0.15% → $3K notional 기준 -$9 입장 손실까지만 인정 (펀딩으로 회복 가능)
        # SOL 사고 케이스: 0.81% spread → -$25 입장 손실, 펀딩으로 24일 걸림 (회복 불가)
        self.URGENT_MAX_UNFAVORABLE_SPREAD_PCT = float(
            os.getenv("URGENT_MAX_UNFAVORABLE_SPREAD_PCT", "0.15")
        )
        self.ENTER_FAVORABLE_TIMEOUT = int(os.getenv("ENTER_FAVORABLE_TIMEOUT", "1800"))
        self.MIN_FUNDING_SPREAD = float(os.getenv("MIN_FUNDING_SPREAD", "0.0005"))
        self.ANALYZE_TIMEOUT = int(os.getenv("ANALYZE_TIMEOUT", "600"))

        # ===== 모니터링 및 안전 설정 =====
        self.POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "3"))  # 초
        self.MARGIN_WARNING_PCT = float(os.getenv("MARGIN_WARNING_PCT", "15"))
        self.MARGIN_EMERGENCY_PCT = float(os.getenv("MARGIN_EMERGENCY_PCT", "10"))
        self.CIRCUIT_BREAKER_FAILS = int(os.getenv("CIRCUIT_BREAKER_FAILS", "5"))
        # HOLD_SUSPENDED: API 장애 시 포지션 유지하며 대기 (양빵 헷지라 당장 위험 없음)
        self.SUSPENDED_ALERT_SECONDS = int(os.getenv("SUSPENDED_ALERT_SECONDS", "300"))  # 5분 후 텔레그램 알림
        self.SUSPENDED_MANUAL_SECONDS = int(os.getenv("SUSPENDED_MANUAL_SECONDS", "1800"))  # 30분 후 MANUAL 전환
        self.PRICE_DIVERGENCE_WARN = float(os.getenv("PRICE_DIVERGENCE_WARN", "3"))
        self.PRICE_DIVERGENCE_EMERGENCY = float(
            os.getenv("PRICE_DIVERGENCE_EMERGENCY", "5")
        )

        # ===== 텔레그램 설정 =====
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

        # ===== 로깅 설정 =====
        self.LOG_SIZE_MB = int(os.getenv("LOG_SIZE_MB", "5"))
        self.LOG_COUNT = int(os.getenv("LOG_COUNT", "3"))
        self.LOG_DIR = Path("logs")

        # ===== 거래 실행 상세 설정 =====
        self.ENTRY_CHUNKS = 5  # 진입 시 5개 청크로 분할 실행
        self.EXIT_CHUNKS = 5  # 청산 시 5개 청크로 분할 실행
        self.CHUNK_RETRY = 2  # 청크 재시도 횟수
        self.CHUNK_WAIT = 30  # 청크 간 대기 시간 (초)
        self.SLIPPAGE_PCT = 0.004  # 0.4% 슬리피지 (taker fallback 가격)
        self.EMERGENCY_SLIPPAGE_PCT = 0.01  # 긴급 상황 1% 슬리피지
        # ===== XEMM 패턴 — GRVT maker → NADO taker (round-trip 약 2bp) =====
        # OI cap 사전 체크 통과한 페어만 진입. 미달 페어는 _oi_blocked 등록 후 다음 페어로.
        self.GRVT_MAKER_OFFSET_PCT = float(os.getenv("GRVT_MAKER_OFFSET_PCT", "0.0001"))  # 시작 backoff 1bp
        self.GRVT_MAKER_TIMEOUT_SECONDS = int(os.getenv("GRVT_MAKER_TIMEOUT_SECONDS", "60"))
        self.GRVT_MAKER_RETRY_LIMIT = int(os.getenv("GRVT_MAKER_RETRY_LIMIT", "5"))
        self.GRVT_MAKER_POLL_INTERVAL_SEC = float(os.getenv("GRVT_MAKER_POLL_INTERVAL_SEC", "2.0"))
        # NADO OI capacity 사전 체크 — 실측 OI vs max_oi에서 5% 버퍼 두고 양수 판정
        self.NADO_OI_BUFFER_PCT = float(os.getenv("NADO_OI_BUFFER_PCT", "0.05"))
        self.MARGIN_BUFFER = 0.65  # 마진 버퍼 (유효마진의 65%, NADO account health 여유 확보)
        self.POLL_BALANCE_SECONDS = 300  # 잔고 폴링 (5분)
        self.POLL_FUNDING_SECONDS = 3600  # 펀딩 폴링 (1시간)

        # ===== 펀딩레이트 설정 =====
        self.LIQUIDITY_CAP_PCT = 0.10  # 오더북 depth의 최대 10%까지만 진입
        self.THIN_MARKET_DEPTH = 500_000  # 이 이하면 얇은 시장 → 청크 대기 2배
        self.MIN_NOTIONAL = 100  # 최소 진입 금액 (USD)

        # NADO API의 funding_rate_x18은 daily(24h) decimal rate를 반환 (검증됨)
        # 정산 cycle은 1h이지만 rate 값 자체는 24h 정규화 형태
        self.NADO_FUNDING_PERIOD_H = 24
        # GRVT는 마켓별 cycle 다름 (BTC=8h, MON=4h 등) — get_funding_rate 내부에서
        # funding_rate_history 타임스탬프로 동적 감지하여 8h decimal로 정규화 반환
        self.GRVT_FUNDING_PERIOD_H = 8  # 정규화 후 형태 (실제 감지된 값 무시)

        # ===== 수수료 설정 (bps, basis points) =====
        self.NADO_MAKER_FEE_BPS = 1.0  # NADO 메이커 수수료 1 bps
        self.GRVT_MAKER_FEE_BPS = -0.01  # (참고) GRVT 메이커 리베이트
        self.GRVT_TAKER_FEE_BPS = 4.5  # GRVT 테이커 4.5 bps (XEMM 모드에서 사용, Tier 1 가정)

    def validate(self) -> list[str]:
        """
        필수 환경변수가 모두 설정되었는지 검증합니다.

        Returns:
            list[str]: 검증 오류 메시지 리스트. 모두 설정되면 빈 리스트.
        """
        errors = []
        required = {
            "NADO_PRIVATE_KEY": self.NADO_PRIVATE_KEY,
            "GRVT_API_KEY": self.GRVT_API_KEY,
            "GRVT_PRIVATE_KEY": self.GRVT_PRIVATE_KEY,
            "GRVT_TRADING_ACCOUNT_ID": self.GRVT_TRADING_ACCOUNT_ID,
        }
        for name, val in required.items():
            if not val:
                errors.append(f"{name} is not set")
        return errors

    def ensure_dirs(self):
        """필요한 디렉토리를 생성합니다."""
        self.LOG_DIR.mkdir(exist_ok=True)

    def mode_params(self, mode: str) -> dict:
        """
        거래 모드별 파라미터를 반환합니다.

        Args:
            mode (str): 모드명 ('HOLD', 'VOLUME', 'VOLUME_URGENT')

        Returns:
            dict: 모드별 파라미터 (min_hold_hours, cooldown, spread_exit)

        Raises:
            KeyError: 존재하지 않는 모드인 경우
        """
        modes = {
            "HOLD": {
                "min_hold_hours": self.MIN_HOLD_HOURS_HOLD,
                "cooldown": self.COOLDOWN_HOLD,
                "spread_exit": self.SPREAD_EXIT_HOLD,
            },
            "VOLUME": {
                "min_hold_hours": self.MIN_HOLD_HOURS_VOLUME,
                "cooldown": self.COOLDOWN_VOLUME,
                "spread_exit": 5.0,   # 수수료 왕복 ~$4 + 소폭 버퍼
            },
            "VOLUME_URGENT": {
                "min_hold_hours": self.MIN_HOLD_HOURS_URGENT,
                "cooldown": self.COOLDOWN_URGENT,
                "spread_exit": 4.0,   # 수수료 왕복 ~$4 타이트 커버
            },
        }
        return modes[mode]

    def estimate_round_trip_fee(self, notional: float) -> float:
        """
        왕복 거래 수수료를 계산합니다 (진입 + 청산).

        Args:
            notional (float): 명목 거래액 (USD)

        Returns:
            float: 예상 왕복 수수료 (USD)
        """
        # XEMM 우선 경로: NADO maker(1bps) × 2 + GRVT maker rebate(-0.01bps) × 2 ≈ 2bps round-trip
        # OI cap 미달 시 fallback (NADO maker → GRVT taker = 11bps) 사용 — 실측은
        # GRVT cumulative_fee로 자동 보정되므로 estimate는 낙관적 기본값 사용.
        nado_fee = notional * 2 * (self.NADO_MAKER_FEE_BPS / 10_000)
        grvt_fee = notional * 2 * (self.GRVT_MAKER_FEE_BPS / 10_000)
        return nado_fee + grvt_fee
