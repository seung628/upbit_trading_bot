# 📱 텔레그램 알림 설정 가이드

## 1️⃣ 텔레그램 봇 생성 (5분)

### Step 1: BotFather와 대화
1. 텔레그램 앱 실행
2. 검색창에 `@BotFather` 입력
3. 대화 시작 `/start`

### Step 2: 봇 생성
```
/newbot

Bot 이름 입력:
QuantPilot Trading Bot

Bot 사용자명 입력:
quantpilot_bot
(반드시 _bot으로 끝나야 함)
```

### Step 3: 토큰 받기
```
Done! Congratulations on your new bot.

Use this token to access the HTTP API:
1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

토큰 복사! (config.json에 사용)
```

---

## 2️⃣ Chat ID 확인

### Step 1: 봇과 대화 시작
1. BotFather가 준 링크 클릭 또는
2. 검색창에 `@quantpilot_bot` 입력
3. `/start` 입력

### Step 2: Chat ID 확인 (2가지 방법)

#### 방법 A: 웹 브라우저 사용
```
1. 브라우저에서 접속:
https://api.telegram.org/bot<토큰>/getUpdates

예시:
https://api.telegram.org/bot1234567890:ABCdefGHI/getUpdates

2. 결과에서 chat id 찾기:
{
  "result": [{
    "message": {
      "chat": {
        "id": 123456789  ← 이것!
      }
    }
  }]
}
```

#### 방법 B: Python 스크립트 사용
```python
# get_chat_id.py
import requests

TOKEN = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

response = requests.get(url)
print(response.json())
```

---

## 3️⃣ config.json 설정

```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
    "chat_id": "123456789",
    "notify_buy": true,
    "notify_sell": true,
    "notify_error": true,
    "notify_daily_summary": true
  }
}
```

---

## 4️⃣ 알림 종류

### 매수 알림
```
🔵 매수 완료

코인: BTC
가격: 60,000,000원
수량: 0.00166
투자: 100,000원
신호: 거래량폭증, MACD골든크로스, RSI과매도
점수: 11점

2026-02-08 10:30:15
```

### 매도 알림
```
🔴 매도 완료

코인: BTC
매수가: 60,000,000원
매도가: 61,500,000원
수익률: +2.5%
수익금: +2,450원

보유시간: 1시간 15분
사유: 1차익절

2026-02-08 11:45:30
```

### 에러 알림
```
⚠️ 오류 발생

유형: 주문 실패
코인: ETH
내용: 잔고 부족

2026-02-08 12:00:00
```

### 일일 요약
```
📊 일일 거래 요약

날짜: 2026-02-08

거래: 15회
승: 10회 (66.7%)
패: 5회

총 손익: +12,500원
최고: BTC +3,450원
최악: XRP -1,520원

현재 잔고: 1,012,500원
```

---

## 5️⃣ 테스트

### 프로그램 시작 시
```
python main.py
> start

텔레그램으로 시작 메시지 전송됨!
```

### 수동 테스트
```python
# test_telegram.py
from telegram_notifier import TelegramNotifier
import json

with open('config.json') as f:
    config = json.load(f)

notifier = TelegramNotifier(config)
notifier.send_message("✅ 테스트 메시지")
```

---

## 6️⃣ 문제 해결

### Q1. 메시지가 안 와요
**A1:** Bot Token 확인
```
config.json의 bot_token이 정확한가?
BotFather에서 받은 토큰과 일치하는가?
```

**A2:** Chat ID 확인
```
/start를 봇에게 보냈는가?
chat_id가 숫자인가? (문자열 아님)
```

**A3:** 인터넷 연결
```
방화벽이 Telegram API를 차단하는가?
```

### Q2. getUpdates가 비어있어요
**A:** 먼저 봇에게 메시지 보내기
```
1. 봇 찾기: @quantpilot_bot
2. /start 입력
3. 아무 메시지나 입력
4. 다시 getUpdates 호출
```

---

## 7️⃣ 보안 주의사항

### ⚠️ Bot Token 보안
```
❌ GitHub에 업로드 금지!
❌ 공개 저장소에 공유 금지!
✅ .gitignore에 config.json 추가
✅ 환경변수 사용 권장
```

### config.json을 GitHub에서 제외
```bash
# .gitignore 파일에 추가
config.json
positions_snapshot.json
trade_history/
logs/
```

---

## 8️⃣ 고급 설정

### 알림 선택적 활성화
```json
{
  "telegram": {
    "enabled": true,
    "notify_buy": true,      // 매수만
    "notify_sell": true,     // 매도만
    "notify_error": false,   // 에러는 끄기
    "notify_daily_summary": true  // 일일 요약만
  }
}
```

### 조용한 알림 (무음)
```json
{
  "telegram": {
    "silent_mode": true  // 알림음 끄기
  }
}
```

---

## 📱 예상 알림 화면

```
[텔레그램 채팅창]

QuantPilot Bot
━━━━━━━━━━━━━━━━

🚀 거래 시작
2026-02-08 09:00:00

━━━━━━━━━━━━━━━━

🔵 매수 완료

코인: BTC
가격: 60,000,000원
수량: 0.00166
투자: 100,000원
━━━━━━━━━━━━━━━━

🔴 매도 완료

코인: BTC
수익: +2,450원 (+2.5%)
━━━━━━━━━━━━━━━━

📊 일일 요약

거래: 15회
승률: 66.7%
수익: +12,500원
```

---

**설정이 완료되면 모든 거래 알림을 실시간으로 받을 수 있습니다!** 📱
