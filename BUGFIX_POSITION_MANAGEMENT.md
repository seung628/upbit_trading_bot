# 🐛 포지션 관리 버그 수정

## 버전: 1.0.3
**날짜:** 2026-02-10
**심각도:** 중간 ⚠️

---

## 🔍 발견된 버그들

### 버그 1: get_current_price 함수 없음
```
AttributeError: 'TradingEngine' object has no attribute 'get_current_price'
```

**발생 위치:** 텔레그램 `/positions` 명령어 실행 시

---

### 버그 2: 포지션 스냅샷 복구 문제
```
[WARNING] ⚠️ KRW-ENSO 포지션은 있으나 실제 잔고 없음
```

**발생 조건:**
1. 프로그램 실행 중 매도 (코인 잔고 0)
2. 프로그램 종료 전 스냅샷 저장 안 됨
3. 재시작 시 스냅샷에는 포지션 있지만 실제 잔고 없음

---

### 버그 3: JSON 직렬화 오류 (잠재적)
```
Object of type datetime is not JSON serializable
```

**원인:** datetime 객체를 JSON으로 직접 저장 시도

---

## ✅ 해결 방법

### 1. get_current_price 함수 추가

#### Before
```python
# trading_engine.py
# 함수 없음! ❌
```

#### After
```python
# trading_engine.py
def get_current_price(self, ticker):
    """현재가 조회"""
    try:
        return pyupbit.get_current_price(ticker)
    except Exception as e:
        self.logger.log_error(f"{ticker} 현재가 조회 오류", e)
        return None
```

**효과:**
- ✅ 텔레그램 `/positions` 정상 작동
- ✅ 실시간 수익률 계산 가능

---

### 2. 포지션 복구 로직 개선

#### Before (문제)
```python
# 포지션 불일치 시 전체 무시
for coin, saved_pos in saved_positions.items():
    actual_balance = get_balance(coin)
    
    if actual_balance <= 0 and saved_amount > 0:
        reconcile_ok = False  # ❌ 전체 무시!

if reconcile_ok:
    positions = saved_positions
else:
    positions = {}  # 전부 버림 ❌
```

**문제점:**
- 하나만 불일치해도 전체 포지션 무시
- 정상 포지션도 손실

#### After (개선)
```python
# 포지션별로 개별 검증 및 정리
reconciled_positions = {}

for coin, saved_pos in saved_positions.items():
    actual_balance = get_balance(coin)
    
    # 실제 잔고 없으면 스냅샷에서 제거
    if actual_balance <= 0:
        logger.warning(f"{coin} 스냅샷에서 제거")
        continue  # ✅ 이것만 제외
    
    # 수량 차이 확인
    if actual_balance > 0:
        diff_pct = abs(actual_balance - saved_amount) / saved_amount * 100
        
        if diff_pct > 5.0:
            # 실제 잔고로 업데이트
            saved_pos['amount'] = actual_balance  # ✅ 자동 수정
            logger.info(f"{coin} 수량 업데이트: {actual_balance}")
        
        reconciled_positions[coin] = saved_pos  # ✅ 복구

# 정리된 포지션 저장
positions = reconciled_positions
save_positions()  # ✅ 스냅샷 업데이트
```

**개선 효과:**
- ✅ 포지션별 개별 검증
- ✅ 실제 잔고 없으면 자동 제거
- ✅ 수량 불일치 시 자동 수정
- ✅ 정상 포지션은 보존
- ✅ 스냅샷 자동 업데이트

---

### 3. JSON 직렬화 안전성 강화

#### save_positions (이미 안전)
```python
def save_positions(self):
    snapshot = {
        'timestamp': datetime.now().isoformat(),  # ✅ 문자열 변환
        'positions': {}
    }
    
    for coin, pos in positions.items():
        snapshot['positions'][coin] = {
            'timestamp': pos['timestamp'].isoformat(),  # ✅ 문자열
            ...
        }
    
    json.dump(snapshot, f)  # ✅ 안전
```

---

## 📊 수정 전후 비교

### 시나리오: 포지션 불일치

#### Before
```
저장된 포지션:
- BTC: 0.001 (정상)
- ETH: 0.05 (정상)
- ENSO: 100 (실제 잔고 0) ❌

복구 결과:
❌ 포지션 불일치 감지!
❌ 전체 포지션 무시
→ BTC, ETH도 손실! 💥
```

#### After
```
저장된 포지션:
- BTC: 0.001 (정상)
- ETH: 0.05 (정상)
- ENSO: 100 (실제 잔고 0) ❌

복구 결과:
✅ BTC 복구 완료
✅ ETH 복구 완료
⚠️ ENSO 스냅샷에서 제거

최종 포지션:
- BTC: 0.001 ✅
- ETH: 0.05 ✅
- ENSO: 제거됨 ✅

스냅샷 업데이트됨 ✅
```

---

## 🧪 테스트 시나리오

### 시나리오 1: 정상 복구
```python
# 저장된 포지션
saved = {
    'BTC': {'amount': 0.001, ...},
    'ETH': {'amount': 0.05, ...}
}

# 실제 잔고
actual_BTC = 0.001  # 일치
actual_ETH = 0.05   # 일치

# 결과
✅ BTC 복구
✅ ETH 복구
```

### 시나리오 2: 잔고 없음
```python
# 저장된 포지션
saved = {
    'BTC': {'amount': 0.001, ...},
    'ENSO': {'amount': 100, ...}
}

# 실제 잔고
actual_BTC = 0.001
actual_ENSO = 0  # ❌ 없음!

# 결과
✅ BTC 복구
⚠️ ENSO 스냅샷에서 제거
✅ 스냅샷 업데이트
```

### 시나리오 3: 수량 불일치
```python
# 저장된 포지션
saved = {
    'BTC': {'amount': 0.001, ...}
}

# 실제 잔고
actual_BTC = 0.0005  # 50% 차이!

# 결과
⚠️ BTC 수량 불일치 감지
✅ 실제 잔고로 업데이트: 0.0005
✅ 복구 완료
```

### 시나리오 4: 텔레그램 positions
```python
# Before
/positions 실행
❌ AttributeError: get_current_price

# After
/positions 실행
✅ 정상 작동

📍 보유 포지션

BTC
매수: 60,000,000원
현재: 61,500,000원
💰 수익: +2.5% (+2,475원)
```

---

## 🔧 코드 변경사항

### 1. trading_engine.py
```python
# Line 602 이후 추가
def get_current_price(self, ticker):
    """현재가 조회"""
    try:
        return pyupbit.get_current_price(ticker)
    except Exception as e:
        self.logger.log_error(f"{ticker} 현재가 조회 오류", e)
        return None
```

### 2. main.py (start 함수)
```python
# Line 89-114 수정
# 포지션 복구 시도
saved_positions = self.stats.load_positions()
if saved_positions:
    reconciled_positions = {}
    
    for coin, saved_pos in saved_positions.items():
        actual_balance = self.engine.upbit.get_balance(coin)
        
        # 실제 잔고 없으면 제거
        if actual_balance <= 0:
            self.logger.warning(f"{coin} 스냅샷에서 제거")
            continue
        
        # 수량 불일치 시 수정
        if actual_balance > 0:
            diff_pct = abs(actual_balance - saved_amount) / saved_amount * 100
            if diff_pct > 5.0:
                saved_pos['amount'] = actual_balance
                self.logger.info(f"{coin} 수량 업데이트")
            
            reconciled_positions[coin] = saved_pos
    
    if reconciled_positions:
        self.stats.positions = reconciled_positions
        self.stats.save_positions()  # 스냅샷 업데이트
```

---

## 📋 영향 범위

### 수정된 파일
- ✅ `trading_engine.py` - get_current_price 추가
- ✅ `main.py` - 포지션 복구 로직 개선

### 영향받는 기능
- ✅ 텔레그램 `/positions` 명령어
- ✅ 프로그램 재시작 시 포지션 복구
- ✅ 포지션 스냅샷 관리

### 영향 없는 부분
- ✅ 매수/매도 로직
- ✅ 신호 감지
- ✅ 통계 기록

---

## 🚀 업그레이드 방법

### 자동 적용
```bash
# 새 파일로 교체
python main.py
> start
```

### 확인 방법
```bash
# 1. 텔레그램 테스트
/positions
→ 정상 작동 확인 ✅

# 2. 로그 확인
tail -f logs/trading_bot.log
→ "스냅샷에서 제거" 메시지 확인

# 3. 스냅샷 파일 확인
cat positions_snapshot.json
→ 실제 잔고 없는 포지션 제거됨 ✅
```

---

## 💡 추가 개선사항

### 1. 자동 정리
- 실제 잔고 없는 포지션 자동 제거
- 스냅샷 자동 업데이트

### 2. 유연한 복구
- 포지션별 개별 검증
- 부분 복구 지원

### 3. 로깅 강화
```
[INFO] 💾 저장된 포지션 발견: 3개
[WARNING] ⚠️ ENSO 스냅샷에서 제거
[INFO] ✅ 포지션 복구 완료: 2개
```

---

## ⚠️ 주의사항

### 수량 불일치 허용 범위
```
5% 이내: 정상 (소수점 오차)
5% 초과: 실제 잔고로 자동 수정
잔고 없음: 스냅샷에서 제거
```

### 스냅샷 업데이트
```
포지션 변경 시마다:
✅ 자동 저장

프로그램 시작 시:
✅ 자동 정리 후 저장
```

---

## 📈 기대 효과

### Before
```
포지션 불일치 발생:
❌ 전체 포지션 손실
❌ 수동 복구 필요
❌ 텔레그램 오류
```

### After
```
포지션 불일치 발생:
✅ 자동 정리
✅ 정상 포지션 보존
✅ 스냅샷 업데이트
✅ 텔레그램 정상 작동
```

---

**이제 포지션 관리가 더욱 견고해졌습니다!** ✅

**실제 잔고와 항상 동기화됩니다!** 🔄

**텔레그램 명령어가 정상 작동합니다!** 📱
