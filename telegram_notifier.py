"""
텔레그램 알림 모듈
"""

import requests
from datetime import datetime
import threading
import time
import re

from version import BOT_NAME, BOT_DISPLAY_NAME, BOT_VERSION


class TelegramNotifier:
    def __init__(self, config):
        self.config = config.get('telegram', {})
        self.enabled = self.config.get('enabled', False)
        
        # 속성 기본값(비활성/설정오류 시에도 접근 가능해야 함)
        self.enable_commands = False
        self.last_update_id = 0
        self.command_thread = None
        self.is_listening = False
        self.command_handler = None
        self.silent_mode = False
        self.poll_timeout_seconds = 3
        self.notify_buy_enabled = True
        self.notify_sell_enabled = True
        self.notify_error_enabled = True
        self.notify_daily_enabled = True
        self.notify_market_enabled = True
        
        # enabled=true인 경우 필수 정보 검증
        if self.enabled:
            self.bot_token = self.config.get('bot_token', '')
            self.chat_id = self.config.get('chat_id', '')
            
            # 필수 정보 누락 체크
            if not self.bot_token or not self.chat_id:
                print("⚠️  텔레그램 설정 불완전: bot_token 또는 chat_id 누락")
                print("   알림 기능이 자동으로 비활성화됩니다.")
                self.enabled = False
            
            # 기본값 그대로인 경우
            elif 'YOUR_BOT_TOKEN' in self.bot_token or 'YOUR_CHAT_ID' in self.chat_id:
                print("⚠️  텔레그램 설정이 필요합니다.")
                print("   config.json에서 bot_token과 chat_id를 설정하세요.")
                self.enabled = False
            
            # 정상 설정된 경우에만 초기화
            else:
                # 토큰/채팅ID 형식 검증
                token_ok = re.match(r'^\d{6,12}:[A-Za-z0-9_-]{30,}$', self.bot_token) is not None
                chat_ok = str(self.chat_id).lstrip('-').isdigit()
                
                if not token_ok:
                    print("⚠️  텔레그램 bot_token 형식이 올바르지 않습니다.")
                    print("   BotFather에서 받은 토큰(예: 123456789:AA...)을 확인하세요.")
                    self.enabled = False
                    return
                
                if not chat_ok:
                    print("⚠️  텔레그램 chat_id 형식이 올바르지 않습니다.")
                    print("   chat_id는 숫자(개인) 또는 -100... (그룹) 형식이어야 합니다.")
                    self.enabled = False
                    return
                
                self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
                
                # 알림 설정
                self.notify_buy_enabled = self.config.get('notify_buy', True)
                self.notify_sell_enabled = self.config.get('notify_sell', True)
                self.notify_error_enabled = self.config.get('notify_error', True)
                self.notify_daily_enabled = self.config.get('notify_daily_summary', True)
                self.notify_market_enabled = self.config.get('notify_market_change', True)
                self.silent_mode = self.config.get('silent_mode', False)
                
                # 명령어 처리
                self.enable_commands = self.config.get('enable_commands', False)
                self.poll_timeout_seconds = int(self.config.get('poll_timeout_seconds', 3))
                
                print("✅ 텔레그램 알림 활성화됨")
    
    def send_message(self, message):
        """텔레그램 메시지 전송"""
        if not self.enabled:
            return False
        
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_notification': self.silent_mode
            }
            
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
            
        except Exception as e:
            print(f"텔레그램 전송 실패: {e}")
            return False
    
    def get_updates(self):
        """새 메시지 확인"""
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                'offset': self.last_update_id + 1,
                'timeout': self.poll_timeout_seconds
            }
            
            response = requests.get(url, params=params, timeout=self.poll_timeout_seconds + 5)
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    return data.get('result', [])
            return []
            
        except Exception as e:
            print(f"업데이트 확인 실패: {e}")
            return []
    
    def start_listening(self, command_handler):
        """명령어 수신 시작"""
        if not self.enabled or not self.enable_commands:
            return False
        
        if self.is_listening:
            return True
        
        self.command_handler = command_handler
        self.is_listening = True
        self.command_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.command_thread.start()
        
        return True
    
    def stop_listening(self):
        """명령어 수신 중지"""
        self.is_listening = False
    
    def _listen_loop(self):
        """명령어 수신 루프"""
        while self.is_listening:
            try:
                updates = self.get_updates()
                
                for update in updates:
                    self.last_update_id = update['update_id']
                    
                    if 'message' in update:
                        message = update['message']
                        
                        # 본인의 메시지만 처리
                        if str(message['chat']['id']) == str(self.chat_id):
                            text = message.get('text', '')
                            
                            if text.startswith('/'):
                                # 명령어 처리
                                if self.command_handler:
                                    self.command_handler(text)
                
                time.sleep(1)  # API 호출 제한
                
            except Exception as e:
                print(f"명령어 수신 오류: {e}")
                time.sleep(5)
    
    def notify_start(
        self,
        bot_name=BOT_NAME,
        bot_version=BOT_VERSION,
        display_name=BOT_DISPLAY_NAME,
        selected_coins=None,
        market_summary_lines=None,
    ):
        """거래 시작 알림"""
        if not self.enabled:
            return
        
        title = display_name or bot_name
        coins = selected_coins or []
        if coins:
            selected_text = ", ".join([coin.replace("KRW-", "") for coin in coins])
        else:
            selected_text = "없음 (자동 재탐색 중)"

        market_lines = market_summary_lines if isinstance(market_summary_lines, list) else []
        market_lines = [str(line) for line in market_lines if line]
        market_block = ""
        if market_lines:
            market_block = "\n🌐 시장 상황:\n" + "\n".join(market_lines)

        message = f"""🚀 <b>거래 시작</b>

봇: {title}
버전: v{bot_version}
시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
초기 선정 종목: {selected_text}
{market_block}

{bot_name}이 자동 매매를 시작합니다.
"""
        self.send_message(message)
    
    def notify_stop(self, final_balance, total_profit):
        """거래 정지 알림"""
        if not self.enabled:
            return
        
        profit_emoji = "📈" if total_profit >= 0 else "📉"
        
        message = f"""⏹️ <b>거래 정지</b>

시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

최종 잔고: {final_balance:,.0f}원
{profit_emoji} 총 손익: {total_profit:+,.0f}원
"""
        self.send_message(message)
    
    def notify_buy(self, ticker, price, amount, invest_amount, signals, score):
        """매수 알림"""
        if not self.enabled:
            return False
        
        if not self.notify_buy_enabled:
            return False
        
        coin_name = ticker.replace('KRW-', '')
        signals_str = ', '.join(signals[:3])  # 최대 3개만
        
        message = f"""🔵 <b>매수 완료</b>

💎 코인: {coin_name}
💰 가격: {price:,.0f}원
📊 수량: {amount:.8f}
💵 투자: {invest_amount:,.0f}원

📈 신호: {signals_str}
⭐ 점수: {score}점

🕐 {datetime.now().strftime('%H:%M:%S')}
"""
        
        success = self.send_message(message)
        
        if not success:
            print(f"⚠️  텔레그램 매수 알림 전송 실패: {ticker}")
        
        return success
    
    def notify_sell(self, ticker, buy_price, sell_price, profit_rate, profit_krw, 
                   holding_time, reason):
        """매도 알림"""
        if not self.enabled:
            return False
        
        if not self.notify_sell_enabled:
            return False
        
        coin_name = ticker.replace('KRW-', '')
        
        # 수익/손실 이모지
        if profit_krw > 0:
            emoji = "💰"
            profit_text = f"+{profit_krw:,.0f}원"
        else:
            emoji = "📉"
            profit_text = f"{profit_krw:,.0f}원"
        
        # 보유 시간 계산
        hours = int(holding_time // 3600)
        minutes = int((holding_time % 3600) // 60)
        
        if hours > 0:
            time_str = f"{hours}시간 {minutes}분"
        else:
            time_str = f"{minutes}분"
        
        message = f"""🔴 <b>매도 완료</b>

💎 코인: {coin_name}
📊 매수가: {buy_price:,.0f}원
📈 매도가: {sell_price:,.0f}원

{emoji} 수익률: {profit_rate:+.2f}%
💵 손익: {profit_text}

⏱️ 보유: {time_str}
📝 사유: {reason}

🕐 {datetime.now().strftime('%H:%M:%S')}
"""
        
        success = self.send_message(message)
        
        if not success:
            print(f"⚠️  텔레그램 매도 알림 전송 실패: {ticker}")
        
        return success
    
    def notify_error(self, error_type, details):
        """에러 알림"""
        if not self.enabled or not self.notify_error_enabled:
            return
        
        message = f"""⚠️ <b>오류 발생</b>

유형: {error_type}
내용: {details}

🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        self.send_message(message)
    
    def notify_daily_summary(self, stats):
        """일일 요약 알림"""
        if not self.enabled or not self.notify_daily_enabled:
            return
        
        total_trades = stats.get('total_trades', 0)
        wins = stats.get('wins', 0)
        losses = stats.get('losses', 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_profit = stats.get('total_profit', 0)
        
        profit_emoji = "📈" if total_profit >= 0 else "📉"
        
        message = f"""📊 <b>일일 거래 요약</b>

📅 {datetime.now().strftime('%Y-%m-%d')}

📊 거래: {total_trades}회
✅ 승: {wins}회
❌ 패: {losses}회
📈 승률: {win_rate:.1f}%

{profit_emoji} 총 손익: {total_profit:+,.0f}원

💰 현재 잔고: {stats.get('current_balance', 0):,.0f}원
"""
        
        # 최고/최악 거래 추가
        if stats.get('best_trade'):
            best = stats['best_trade']
            message += f"\n🏆 최고: {best['coin']} {best['profit']:+,.0f}원"
        
        if stats.get('worst_trade'):
            worst = stats['worst_trade']
            message += f"\n📉 최악: {worst['coin']} {worst['profit']:+,.0f}원"
        
        self.send_message(message)
    
    def notify_cooldown(self, reason, minutes):
        """쿨다운 알림"""
        if not self.enabled:
            return
        
        message = f"""❄️ <b>거래 일시 정지</b>

사유: {reason}
재개: {minutes}분 후

        🕐 {datetime.now().strftime('%H:%M:%S')}
"""
        self.send_message(message)

    def _regime_label(self, regime):
        value = str(regime or "").upper()
        labels = {
            "BULL": "상승장(BULL)",
            "BEAR": "하락장(BEAR)",
            "RANGE": "횡보장(RANGE)",
        }
        return labels.get(value, value or "알 수 없음")

    def notify_market_change(self, previous_regime, current_regime, detect_meta=None, confirm_count=None):
        """시장 국면(레짐) 변경 알림"""
        if not self.enabled or not self.notify_market_enabled:
            return False

        prev = self._regime_label(previous_regime)
        curr = self._regime_label(current_regime)
        meta = detect_meta if isinstance(detect_meta, dict) else {}

        ref = meta.get("reference_ticker", "KRW-BTC")
        close = meta.get("close")
        ema50 = meta.get("ema50")
        ema200 = meta.get("ema200")

        market_line = f"기준: {ref}"
        try:
            if close is not None and ema50 is not None and ema200 is not None:
                market_line = (
                    f"기준: {ref}\n"
                    f"종가: {float(close):,.0f} | EMA50: {float(ema50):,.0f} | EMA200: {float(ema200):,.0f}"
                )
        except Exception:
            pass

        confirm_text = ""
        if confirm_count is not None:
            try:
                confirm_text = f"\n전환 확정 조건: {int(confirm_count)}회 연속 확인"
            except Exception:
                confirm_text = ""

        message = (
            "🌐 <b>시장 상황 변경</b>\n\n"
            f"{prev} ➜ {curr}\n"
            f"{market_line}"
            f"{confirm_text}\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return self.send_message(message)
    
    def test_connection(self):
        """연결 테스트"""
        if not self.enabled:
            return False, "텔레그램 알림이 비활성화되어 있습니다"
        
        try:
            url = f"{self.base_url}/getMe"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    bot_name = data['result'].get('username', 'Unknown')
                    return True, f"연결 성공: @{bot_name}"
                else:
                    return False, "봇 토큰이 잘못되었습니다"
            else:
                return False, f"HTTP 오류: {response.status_code}"
                
        except Exception as e:
            return False, f"연결 실패: {e}"
