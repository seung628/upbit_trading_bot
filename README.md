# upbit_trading_bot

업비트 KRW 현물 자동매매 봇입니다.  
현재 기본 전략은 `regime_spec_v2`(SOL/DOGE/ADA 고정 유니버스)입니다.

## 현재 전략 요약

- 유니버스: `KRW-SOL`, `KRW-DOGE`, `KRW-ADA`
- 최대 동시 포지션: 설정값(`strategy.max_positions`, 기본 3)
- 신규 진입 차단 시간: 02:00~06:00 (로컬 서버 시간)
- BTC 필터: 20분봉 기준 `KRW-BTC 종가 > EMA50`
- 변동성 필터: `TR / ATR(14) <= 3.0`
- 레짐 판단(20분봉):
  - `BULL`: Close > EMA50 > EMA200
  - `BEAR`: Close < EMA50 < EMA200
  - `RANGE`: 그 외
  - 전환 확정: 3개 캔들 연속 확인

## 종목별 진입/청산

### SOL (`SOL_TREND`)
- 조건: `BULL` 레짐 + 최근 48봉 고점 돌파 + ATR 기반 리테스트
- 손절: `entry - 0.5 * ATR`
- 익절: 1.2R에서 30% 분할, 2.2R 이후 트레일링(잔여 70%)

### DOGE (`DOGE_MOMENTUM`)
- 조건: 거래량 스파이크(현재 >= 20봉 평균 * 1.3) + RSI > 55 + EMA20 풀백
- 손절: `entry * (1 - 0.008)`
- 시간청산: 6캔들 내 목표 미도달 시 청산

### ADA (`ADA_RANGE`)
- 조건: `RANGE` 레짐 + RSI <= 28 + 96봉 범위 하단 15% 구간
- 손절: `entry * (1 - 0.009)`
- 익절: 96봉 범위 상단 85% 가격 도달 시 청산

## 리스크/자금 운용

- 수수료 기본값: 0.05% (편도)
- 종목별 리스크(계좌 기준):
  - SOL 0.5%
  - DOGE 0.4%
  - ADA 0.3%
- 포지션 사이징:
  - `position_size = (account_balance * risk_pct) / stop_distance`
- 총 투자한도: `trading.max_total_investment`
- 최소 주문금액: `trading.min_trade_amount`

## 로그/분석

- `logs/decisions.log` (JSONL): 진입 차단 사유, 사이징, 체결, 청산 메타
- `trade_history/YYYYMMDD.json`: 거래 내역 영속 저장
- 거래 레코드 주요 필드:
  - `entry_time`, `exit_time`
  - `symbol`, `strategy`
  - `entry_price`, `stop_price`, `position_size`, `exit_price`
  - `realized_pnl_krw`, `r_multiple`

## 텔레그램

- 시작/매수/매도/오류/일일 요약 알림 지원
- 레짐 전환 시 시장 상황 변경 알림 지원 (`telegram.notify_market_change`)
- 명령어: `/status`, `/daily`, `/weekly`, `/positions`, `/balance`, `/pause`, `/resume`, `/version`, `/help`

## 실행 방법

### 1) 설치

```bash
pip install -r requirements.txt
```

### 2) 설정

`config.example.json`을 참고해 `config.json` 작성

필수:
- `api.access_key`
- `api.secret_key`

권장:
- `strategy.symbol_strategy_map` (종목별 전략/레짐 매핑)

### 3) 실행

```bash
python main.py
```

`trading.auto_start_on_launch=true`면 실행 직후 자동으로 트레이딩이 시작됩니다.

## 현재 사용되는 주요 설정 키

`strategy`:
- `symbol_strategy_map`
- `universe`
- `max_positions`

`trading`:

- `max_total_investment`
- `analysis_heartbeat_minutes`
- `fee_pct`
- `reentry_cooldown_after_stoploss_minutes`
- `untracked_balance.action`
- `untracked_balance.cleanup_max_krw`
- `min_orderbook_depth_krw`
- `max_spread_percent`
- `min_trade_amount`
- `check_interval_seconds`
- `order_type`
- `limit_order_wait_seconds`
- `daily_loss_limit_percent`
- `cooldown_after_loss_minutes`
- `auto_start_on_launch`
- `trading_hours.enabled`
- `trading_hours.sessions`

## 참고

- 본 프로젝트는 투자 손실을 방지하지 않습니다.
- 실거래 전 소액/모의 검증을 권장합니다.
