# Lessons Learned

## 2026-04-30: API 503 과민 반응으로 편측 청산 사고

### 문제
- GRVT API 503(간헐적, 30초~1분) → circuit breaker 15초 만에 발동 → emergency_exit
- emergency_exit에서 GRVT API도 안 됨 → NADO만 청산 → 편측 노출

### 근본 원인
- **양빵 헷지 포지션은 한쪽 API 장애에도 당장 위험하지 않음**인데,
  circuit breaker가 이를 "위험 상황"으로 과대 판정하여 즉시 emergency_exit을 트리거
- 503은 서버 일시적 과부하/점검이라 기다리면 복구되는데, 15초 만에 반응한 것이 문제

### 해결
- circuit_breaker → emergency_exit 경로 **삭제**
- 대신 **HOLD_SUSPENDED** 상태로 전환 (포지션 유지, API 복구 대기)
- 복구 감지 → HOLD 자동 복귀
- 30분 미복구 → MANUAL_INTERVENTION (사람이 확인)

### 규칙 (앞으로)
1. **양빵 전략에서 emergency_exit은 "양쪽 가격 괴리" 등 진짜 위험만** 사용
2. **API 장애 = 기다리면 되는 문제** → 급히 청산하지 않기
3. **dust retry 루프에서도 "GRVT 먼저 → 성공해야 NADO"** 순서 지키기
4. **편측 청산 금지 가드**는 2차 방어선으로 유지
