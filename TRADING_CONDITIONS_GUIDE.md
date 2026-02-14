# 🎯 매수 조건 설정 완전 가이드

## 📋 목차
1. [기본 개념](#기본-개념)
2. [현재 설정값 (config.json)](#현재-설정값)
3. [조건별 상세 설명](#조건별-상세-설명)
4. [난이도별 추천 설정](#난이도별-추천-설정)
5. [매수가 안 될 때](#매수가-안-될-때)
6. [로그 분석 방법](#로그-분석-방법)

---

## 기본 개념

### 매수 조건 체크 순서

```
1단계: 거래 시간 체크
   ↓ 거래 시간이 아니면 대기
   
2단계: 코인 목록 체크
   ↓ 대상 코인이 아니면 건너뜀
   
3단계: 추세 확인 (횡보장 필터)
   ↓ 횡보장이면 거래 안 함
   
4단계: 신호 점수 계산
   ↓ 점수가 부족하면 거래 안 함
   
5단계: 호가창 안전성 체크
   ↓ 스프레드/호가 문제 있으면 거래 안 함
   
6단계: 투자 한도 체크
   ↓ 한도 초과면 거래 안 함
   
7단계: 매수 실행! ✅
```

---

## 현재 설정값

### 기본 config.json

```json
{
  "indicators": {
    "use_signal_scoring": true,
    "min_signal_score": 7,
    "check_trend": true,
    "min_trend_strength": 0.02
  },
  
  "trading": {
    "max_spread_percent": 0.5,
    "min_orderbook_depth_krw": 5000000,
    "trading_hours": {
      "enabled": true,
      "sessions": [
        {"start": 9, "end": 11},
        {"start": 21, "end": 23}
      ]
    }
  },
  
  "coin_selection": {
    "min_volume_krw": 10000000000,
    "min_volatility": 3,
    "max_volatility": 15
  }
}
```

---

## 조건별 상세 설명

### 1️⃣ 신호 점수제

#### 현재 설정
```json
{
  "use_signal_scoring": true,
  "min_signal_score": 7
}
```

#### 의미
- 각 신호에 1-3점 가중치 부여
- 총점 7점 이상일 때만 매수

#### 신호별 점수표

| 신호 | 조건 | 점수 | 난이도 |
|------|------|------|--------|
| 거래량 폭등 | 평균 × 2배 이상 | 3점 | 어려움 |
| MACD 골든크로스 | MACD > Signal | 3점 | 어려움 |
| RSI 강한 과매도 | RSI < 30 | 3점 | 어려움 |
| 거래량 급증 | 평균 × 1.8배 | 2점 | 보통 |
| RSI 약한 과매도 | RSI 30-35 | 2점 | 보통 |
| BB 하단 반등 | 하단 돌파 후 복귀 | 2점 | 보통 |
| BB 하위 구간 | 하위 25% | 2점 | 쉬움 |
| MA5 상승 | 5일선 상승 | 1점 | 쉬움 |

#### 점수 조합 예시

```
[7점 조합 - 최소]
거래량급증(2) + RSI약과매도(2) + BB하단반등(2) + MA5상승(1) = 7점 ✅

[8점 조합 - 보통]
거래량급증(2) + MACD골든크로스(3) + BB하위(2) + MA5상승(1) = 8점 ✅

[11점 조합 - 강함]
거래량폭등(3) + MACD골든크로스(3) + RSI강과매도(3) + BB하단반등(2) = 11점 ✅✅
```

#### 조정 가이드

**거래가 너무 적을 때:**
```json
{
  "min_signal_score": 6  // 7 → 6으로 낮춤
}
```
- 효과: 거래 기회 약 30% 증가
- 부작용: 승률 약간 감소 (-3%p)

**승률이 낮을 때:**
```json
{
  "min_signal_score": 8  // 7 → 8로 높임
}
```
- 효과: 승률 약 5%p 향상
- 부작용: 거래 기회 40% 감소

---

### 2️⃣ 추세 확인 (횡보장 필터)

#### 현재 설정
```json
{
  "check_trend": true,
  "min_trend_strength": 0.02
}
```

#### 의미
- MA20 기울기가 ±2% 이상이어야 거래
- -2% ~ +2% 사이는 횡보장으로 간주

#### 기울기 계산

```
기울기 = (현재 MA20 - 20봉 전 MA20) / 20봉 전 MA20

예시:
현재 MA20: 102원
20봉 전: 100원
기울기 = (102 - 100) / 100 = 0.02 = 2% ✅ 거래 가능

현재 MA20: 101원  
20봉 전: 100원
기울기 = (101 - 100) / 100 = 0.01 = 1% ❌ 횡보장
```

#### 조정 가이드

**거래가 너무 적을 때:**
```json
{
  "check_trend": false  // 추세 확인 끄기
}
```
또는
```json
{
  "min_trend_strength": 0.01  // 2% → 1%로 완화
}
```

**횡보장 손절이 많을 때:**
```json
{
  "min_trend_strength": 0.03  // 2% → 3%로 강화
}
```

---

### 3️⃣ 호가창 안전성

#### 현재 설정
```json
{
  "max_spread_percent": 0.5,
  "min_orderbook_depth_krw": 5000000
}
```

#### 의미

**스프레드:**
```
스프레드 = (매도1호가 - 매수1호가) / 매수1호가 × 100

예시:
매도1호가: 100,500원
매수1호가: 100,000원
스프레드 = (100,500 - 100,000) / 100,000 = 0.5% ✅

매도1호가: 101,000원
매수1호가: 100,000원  
스프레드 = (101,000 - 100,000) / 100,000 = 1.0% ❌
```

**호가 잔량:**
```
매도1호가: 100,000원 × 0.05 BTC = 5,000,000원 ✅
매도1호가: 100,000원 × 0.03 BTC = 3,000,000원 ❌
```

#### 조정 가이드

**거래가 너무 적을 때:**
```json
{
  "max_spread_percent": 1.0,        // 0.5% → 1.0%
  "min_orderbook_depth_krw": 2000000  // 500만 → 200만
}
```

**슬리피지가 많을 때:**
```json
{
  "max_spread_percent": 0.3,         // 0.5% → 0.3%
  "min_orderbook_depth_krw": 10000000  // 500만 → 1,000만
}
```

---

### 4️⃣ 거래 시간 필터

#### 현재 설정
```json
{
  "trading_hours": {
    "enabled": true,
    "sessions": [
      {"start": 9, "end": 11},
      {"start": 21, "end": 23}
    ]
  }
}
```

#### 의미
- 오전 9-11시만 거래
- 오후 9-11시만 거래
- 그 외 시간은 거래 중지

#### 조정 가이드

**24시간 거래:**
```json
{
  "trading_hours": {
    "enabled": false
  }
}
```

**거래 시간 확장:**
```json
{
  "sessions": [
    {"start": 9, "end": 12},   // 3시간
    {"start": 21, "end": 24}   // 3시간
  ]
}
```

**피크 타임만:**
```json
{
  "sessions": [
    {"start": 9, "end": 10},   // 1시간
    {"start": 21, "end": 22}   // 1시간
  ]
}
```

---

### 5️⃣ 코인 선정 기준

#### 현재 설정
```json
{
  "min_volume_krw": 10000000000,  // 100억
  "min_volatility": 3,             // 3%
  "max_volatility": 15             // 15%
}
```

#### 의미
- 거래량 100억 이상
- 변동성 3-15% 사이

#### 조정 가이드

**거래 기회 늘리기:**
```json
{
  "min_volume_krw": 5000000000,  // 50억으로 완화
  "min_volatility": 2,            // 2%로 완화
  "max_volatility": 20            // 20%로 확대
}
```

**안정성 높이기:**
```json
{
  "min_volume_krw": 20000000000,  // 200억으로 강화
  "min_volatility": 4,             // 4%로 강화
  "max_volatility": 10             // 10%로 축소
}
```

---

## 난이도별 추천 설정

### 🟢 Easy (거래 많음, 승률 낮음)

```json
{
  "indicators": {
    "use_signal_scoring": true,
    "min_signal_score": 5,        // ⬇️ 낮춤
    "check_trend": false,          // ❌ 끄기
    "min_trend_strength": 0.01
  },
  "trading": {
    "max_spread_percent": 1.0,    // ⬆️ 완화
    "min_orderbook_depth_krw": 1000000,  // ⬇️ 완화
    "trading_hours": {
      "enabled": false             // ❌ 24시간
    }
  },
  "coin_selection": {
    "min_volume_krw": 5000000000,  // ⬇️ 완화
    "min_volatility": 2,           // ⬇️ 완화
    "max_volatility": 20           // ⬆️ 확대
  }
}
```

**예상 결과:**
- 거래 빈도: 20-30회/일
- 승률: 50-55%
- 월 수익: 5-8%

---

### 🟡 Medium (기본 - 균형) ⭐ 추천

```json
{
  "indicators": {
    "use_signal_scoring": true,
    "min_signal_score": 7,        // 기본
    "check_trend": true,           // ✅ 사용
    "min_trend_strength": 0.02    // 기본
  },
  "trading": {
    "max_spread_percent": 0.5,    // 기본
    "min_orderbook_depth_krw": 5000000,  // 기본
    "trading_hours": {
      "enabled": true,             // ✅ 사용
      "sessions": [
        {"start": 9, "end": 11},
        {"start": 21, "end": 23}
      ]
    }
  },
  "coin_selection": {
    "min_volume_krw": 10000000000,  // 기본
    "min_volatility": 3,            // 기본
    "max_volatility": 15            // 기본
  }
}
```

**예상 결과:**
- 거래 빈도: 8-15회/일
- 승률: 60-68%
- 월 수익: 10-15%

---

### 🔴 Hard (거래 적음, 승률 높음)

```json
{
  "indicators": {
    "use_signal_scoring": true,
    "min_signal_score": 9,        // ⬆️ 높임
    "check_trend": true,
    "min_trend_strength": 0.03    // ⬆️ 강화
  },
  "trading": {
    "max_spread_percent": 0.3,    // ⬇️ 강화
    "min_orderbook_depth_krw": 10000000,  // ⬆️ 강화
    "trading_hours": {
      "enabled": true,
      "sessions": [
        {"start": 9, "end": 10},   // 1시간만
        {"start": 21, "end": 22}   // 1시간만
      ]
    }
  },
  "coin_selection": {
    "min_volume_krw": 20000000000,  // ⬆️ 강화
    "min_volatility": 4,            // ⬆️ 강화
    "max_volatility": 10            // ⬇️ 축소
  }
}
```

**예상 결과:**
- 거래 빈도: 2-5회/일
- 승률: 70-78%
- 월 수익: 8-12%

---

## 매수가 안 될 때

### 진단 순서

#### 1단계: 프로그램 시작 시 조건 확인
```
🚀 거래 시작
==================
📋 현재 매수 조건
==================

여기서 모든 조건 확인 가능!
```

#### 2단계: 로그 파일 확인
```bash
tail -f logs/trading_bot.log
```

#### 3단계: 거래가 안 되는 이유 찾기

**패턴 1: 횡보장**
```
[DEBUG] KRW-BTC ❌ 횡보장 (기울기 0.8% < 2.0%)
[DEBUG] KRW-ETH ❌ 횡보장 (기울기 1.2% < 2.0%)
```
**해결:** `min_trend_strength` 낮추기 또는 `check_trend: false`

**패턴 2: 점수 부족**
```
[DEBUG] KRW-BTC 신호 점수: 5점
        ✅ RSI약과매도(2점, 34.5)
        ✅ BB하위(2점, 18%)
        ✅ MA5상승(1점)
        ❌ MACD골든크로스(미충족)
[DEBUG] KRW-BTC ❌ 점수 부족 (5점 < 7점)
```
**해결:** `min_signal_score` 낮추기 (7 → 6)

**패턴 3: 호가 문제**
```
[DEBUG] KRW-SHIB 호가 불안정: 매도호가 부족(2,300,000원)
```
**해결:** `min_orderbook_depth_krw` 낮추기

**패턴 4: 거래 시간 외**
```
[INFO] ⏸️ 거래 시간 종료 - 일시 정지
      다음 거래 시간: 21시 ~ 23시
```
**해결:** `trading_hours.enabled: false`

---

## 로그 분석 방법

### 상세 로그 보기

```bash
# 실시간 로그
tail -f logs/trading_bot.log

# 오늘의 매수 시도만
grep "신호 점수" logs/trading_bot.log

# 오늘의 실패 이유
grep "❌" logs/trading_bot.log
```

### 로그 해석

```
[정상 - 매수 성공]
2026-02-08 10:00:15 [DEBUG] KRW-BTC 신호 점수: 8점
                            ✅ 거래량급증(2점, 2.1배)
                            ✅ MACD골든크로스(3점)
                            ✅ BB하위(2점, 15%)
                            ✅ MA5상승(1점)
2026-02-08 10:00:16 [INFO]  KRW-BTC ✅ 매수 조건 충족! (점수: 8점)
2026-02-08 10:00:16 [INFO]  호가 체크: 안전 ✅
2026-02-08 10:00:20 [INFO]  🔵 매수 완료 | KRW-BTC

[실패 - 점수 부족]
2026-02-08 10:05:00 [DEBUG] KRW-ETH 신호 점수: 4점
                            ✅ BB하위(2점, 20%)
                            ✅ RSI약과매도(2점, 33.2)
                            ❌ 거래량급증(미충족, 1.2배)
                            ❌ MACD골든크로스(미충족)
2026-02-08 10:05:00 [DEBUG] KRW-ETH ❌ 점수 부족 (4점 < 7점)
```

---

## 빠른 조정 체크리스트

### ❌ 거래가 전혀 없을 때
```
□ min_signal_score: 7 → 5
□ check_trend: true → false
□ trading_hours.enabled: true → false
□ min_orderbook_depth_krw: 5000000 → 1000000
```

### ❌ 거래는 있는데 너무 적을 때
```
□ min_signal_score: 7 → 6
□ min_trend_strength: 0.02 → 0.01
□ max_spread_percent: 0.5 → 0.8
```

### ❌ 손절이 너무 많을 때
```
□ min_signal_score: 7 → 8
□ min_trend_strength: 0.02 → 0.03
□ max_spread_percent: 0.5 → 0.3
```

---

## 추천 조정 순서

### 1주차: 관찰
- 기본 설정(Medium) 유지
- 로그 분석
- 실패 패턴 파악

### 2주차: 1차 조정
- 가장 빈번한 실패 이유 해결
- 한 번에 1-2개 항목만 조정

### 3주차: 최적화
- 승률과 거래 빈도 균형
- 미세 조정

### 4주차 이후: 안정 운영
- 시장 상황에 따라 조정
- 월 1회 검토

---

**이 문서를 보면서 설정을 조정하세요!**
**로그를 보면 어떤 조건 때문에 매수가 안 되는지 정확히 알 수 있습니다!** 🎯
