# upbit_trading_bot

업비트 자동매매 봇입니다.  
현재 버전은 `v1.2.0`이며, 비용(수수료/스프레드)과 체결 품질을 우선하는 단기 추세 전략으로 동작합니다.

## 핵심 개요

- 전략: 비용 민감 추세 돌파 (`trend_breakout`)
- 진입: 1분 + 15분 추세 정렬, 돌파, 거래량, RSI, 점수 게이팅
- 청산: 고정/ATR 손절, 수익구간 트레일링, 추세 이탈, 최대 보유시간
- 자금 운용: 총 투자한도 고정 + 슬롯 기반 + 신호 점수 + 리스크(ATR/스프레드) 가중
- 안전장치: 호가 스프레드/깊이 필터, 일일 손실 제한, 손절 후 재진입 쿨다운
- 운영: 텔레그램 알림/명령, 일일/주간 리포트, 포지션 스냅샷 복구

## v1.2.0 변경 사항

- 종목 선정 로직 고도화
  - 1분봉 추세 정렬/돌파 근접도/거래량 추세 반영 점수화
  - 상위 후보에 대해 호가 품질(스프레드/깊이) 사전 필터 적용
  - RSI 범위 우선 정책 유지 + 부족 시 완화 fallback
- 수량/투자금 로직 개선
  - `max_total_investment`를 항상 상한으로 준수
  - `max_position_ratio`로 종목 과집중 제한
  - 남은 슬롯 기반 배분 + 신호 점수 가중 + ATR/스프레드 리스크 스케일 적용
- 불필요 재시도 정리
  - 동일 확정봉에서 중복 매수 시도 스킵
- 안정성 개선
  - 잔고 조회/호가 가격 0 예외 처리 강화
  - 로거 재초기화 시 핸들러 중복 방지

## 전략 상세

### 1) 종목 선정

- 기본 필터
  - KRW 마켓
  - 최소 거래대금 (`coin_selection.min_volume_krw`)
  - 변동성 범위 (`min_volatility`~`max_volatility`)
  - 제외 종목 (`excluded_coins`)
- 점수 요소
  - 거래대금/거래량 추세
  - BB 폭(너무 좁거나 과도한 구간 회피)
  - RSI/RSI 기울기
  - 단기 추세 정렬(EMA fast > EMA slow)
  - 돌파 근접도(과도한 추격 방지)
- 체결 품질 필터(상위 후보 대상)
  - 스프레드
  - 매수/매도 호가 깊이

### 2) 매수 규칙

- `minute1`: 가격 > EMA20 > EMA60
- `minute15`: 가격 > EMA20 > EMA50
- 최근 N봉 고점 돌파 + 버퍼
- 거래량 비율 임계치 이상
- RSI 범위 (`entry_rsi_min <= RSI < entry_rsi_max`)
- 종합 점수 `entry_min_score` 이상

### 3) 매도 규칙

- 손절: 고정 손절 + ATR 손절 중 덜 타이트한 기준
- 트레일링: 수익 구간에서만 활성
- 최소 보유시간 이전 소프트 청산 금지
- 추세 이탈(1분 + 상위/RSI 약세) 시 청산
- 최대 보유시간 도달 시 수익보호 청산

### 4) 자금/수량 규칙

- 총 투자 한도: `trading.max_total_investment`
- 종목당 최대 비중: `trading.max_position_ratio`
- 동적 배분
  - 남은 슬롯 수 기반 예산 배분
  - 신호 점수 가중
  - ATR%/스프레드 기반 리스크 조정
  - 최소 주문금액 보장 및 잔여 슬롯 예산 예약

## 빠른 시작

### 1) 설치

```bash
pip install -r requirements.txt
```

### 2) 설정

`config.example.json`을 참고해서 `config.json`을 작성합니다.

필수:

- `api.access_key`
- `api.secret_key`

### 3) 실행

```bash
python main.py
```

자동 시작이 활성화(`trading.auto_start_on_launch=true`)되어 있으면 즉시 거래 루프가 시작됩니다.

## 주요 설정 키

### trading

- `max_coins`: 동시 타깃 종목 수
- `buy_amount_krw`: 기준 매수 금액
- `max_total_investment`: 총 투자 한도
- `max_position_ratio`: 종목당 최대 비중(한도 대비)
- `dynamic_allocation`: 동적 배분 사용 여부
- `max_spread_percent`: 허용 스프레드
- `min_orderbook_depth_krw`: 최소 호가 깊이
- `daily_loss_limit_percent`: 일일 손실 제한

### coin_selection

- `min_volume_krw`, `min_volatility`, `max_volatility`
- `selection_shortlist_multiplier`: 호가 필터 전 후보 배수
- `orderbook_filter_enabled`: 선정 단계 호가 필터 사용
- `require_trend_alignment`: 종목 선정 시 추세 정렬 강제
- `excluded_coins`: 보호/제외 종목

### strategy

- `entry_interval`, `htf_interval`
- `entry_breakout_lookback`, `entry_breakout_buffer_pct`
- `entry_volume_ratio_min`
- `entry_rsi_min`, `entry_rsi_max`
- `entry_ma_fast`, `entry_ma_slow`
- `htf_ma_fast`, `htf_ma_slow`
- `entry_min_score`

### risk_management

- `use_atr`, `atr_period`
- `stop_loss_pct`, `min_atr_stop_loss_pct`
- `trailing_stop_pct`, `trailing_activation_pct`
- `min_hold_minutes`, `max_hold_minutes`
- `use_partial_take_profit`

## 콘솔 명령어

- `start`: 거래 시작
- `stop`: 거래 정지(포지션 정리)
- `status`: 현재 상태
- `daily`: 일일 통계
- `weekly`: 주간 통계
- `refresh`: 종목 재선정
- `version`: 버전 확인
- `help`: 도움말
- `exit`: 종료

## 텔레그램 명령어

- `/status`
- `/daily`
- `/weekly`
- `/positions`
- `/balance`
- `/refresh`
- `/pause`
- `/resume`
- `/version`
- `/help`

## 로그/데이터 파일

- `logs/trading_bot.log`: 메인 로그
- `logs/decisions.log`: 의사결정 JSONL 로그
- `logs/trades.log`: 체결 로그
- `trade_history/YYYYMMDD.json`: 일자별 거래 내역
- `positions_snapshot.json`: 포지션 스냅샷

## 운영 권장

- 과최적화보다 일관된 리스크 관리와 비용 통제를 우선하세요.
- 설정 변경 후 최소 수일 이상 로그(`decisions.log`, `trade_history`)로 검증하세요.
- 실거래 전 소액 테스트를 권장합니다.

## 주의

이 소프트웨어는 투자 손실을 방지하지 않습니다.  
모든 투자 판단과 책임은 사용자에게 있습니다.
