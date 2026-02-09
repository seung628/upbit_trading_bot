"""
업비트 자동 매매 봇 - 메인 실행 파일
"""

import json
import time
import threading
from datetime import datetime, timedelta
import os
import sys
import readline  # 명령어 히스토리용

# 로컬 모듈 임포트
from logger import TradingLogger
from trading_stats import TradingStats
from coin_selector import CoinSelector
from trading_engine import TradingEngine
from telegram_notifier import TelegramNotifier


class TradingBot:
    def __init__(self, config_path='config.json'):
        # 설정 로드
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        
        # 모듈 초기화
        self.logger = TradingLogger(self.config)
        self.stats = TradingStats()
        self.coin_selector = CoinSelector(self.config, self.logger)
        self.engine = TradingEngine(self.config, self.logger, self.stats)
        self.telegram = TelegramNotifier(self.config)
        
        # 상태 변수
        self.is_running = False
        self.trading_thread = None
        self.target_coins = []
        self.last_coin_refresh = None
        self.is_trading_paused = False
        self.cooldown_until = None  # 쿨다운 종료 시간
        
        # 중복 매수 방지
        self.buying_in_progress = set()  # 현재 매수 중인 코인들
        self.buy_lock = threading.Lock()  # 매수 Lock
        
        # 설정값
        self.max_coins = self.config['trading']['max_coins']
        self.buy_amount_krw = self.config['trading']['buy_amount_krw']
        self.max_total_investment = self.config['trading'].get('max_total_investment', 300000)
        self.dynamic_allocation = self.config['trading'].get('dynamic_allocation', False)
        self.check_interval = self.config['trading']['check_interval_seconds']
        self.refresh_interval_hours = self.config['coin_selection'].get('refresh_interval_hours', 1)
        
        # 일일 손실 제한
        self.daily_loss_limit = self.config['trading'].get('daily_loss_limit_percent', -5.0)
        self.cooldown_minutes = self.config['trading'].get('cooldown_after_loss_minutes', 30)
        
        # 거래 시간 필터
        self.trading_hours_enabled = self.config['trading']['trading_hours'].get('enabled', False)
        self.trading_sessions = self.config['trading']['trading_hours'].get('sessions', [])
    
    def start(self):
        """트레이딩 시작"""
        
        if self.is_running:
            print("⚠️  이미 실행 중입니다.")
            return
        
        self.logger.info("="*80)
        self.logger.info("🚀 업비트 자동매매 봇 시작")
        self.logger.info("="*80)
        
        # API 연결
        if not self.engine.connect(
            self.config['api']['access_key'],
            self.config['api']['secret_key']
        ):
            print("❌ API 연결 실패. 설정을 확인하세요.")
            return
        
        # 초기 잔고 확인
        initial_balance = self.engine.get_balance("KRW")
        if initial_balance < self.config['trading']['min_trade_amount']:
            print(f"❌ 거래 가능 금액이 부족합니다. (최소 {self.config['trading']['min_trade_amount']:,}원)")
            return
        
        self.stats.start(initial_balance)
        
        # 포지션 복구 시도
        saved_positions = self.stats.load_positions()
        if saved_positions:
            self.logger.info(f"💾 저장된 포지션 발견: {len(saved_positions)}개")
            
            # 계정 잔고와 대조 (Reconcile)
            reconcile_ok = True
            for coin, saved_pos in saved_positions.items():
                actual_balance = self.engine.upbit.get_balance(coin)
                saved_amount = saved_pos['amount']
                
                # 차이가 1% 이상이면 경고
                if actual_balance > 0:
                    diff_pct = abs(actual_balance - saved_amount) / saved_amount * 100
                    if diff_pct > 1.0:
                        self.logger.warning(f"⚠️  {coin} 수량 불일치: 저장 {saved_amount:.8f} vs 실제 {actual_balance:.8f}")
                        reconcile_ok = False
                elif saved_amount > 0:
                    self.logger.warning(f"⚠️  {coin} 포지션은 있으나 실제 잔고 없음")
                    reconcile_ok = False
            
            if reconcile_ok:
                self.stats.positions = saved_positions
                self.logger.info(f"✅ 포지션 복구 완료")
            else:
                self.logger.error("❌ 포지션 불일치 감지! 저장된 포지션 무시")
        
        # 코인 선정
        self.target_coins = self.coin_selector.get_top_coins(self.max_coins)
        self.last_coin_refresh = datetime.now()
        
        if not self.target_coins:
            print("❌ 거래 가능한 코인이 없습니다.")
            return
        
        # 거래 시작
        self.is_running = True
        self.trading_thread = threading.Thread(target=self._trading_loop, daemon=True)
        self.trading_thread.start()
        
        # 매수 조건 출력
        self._print_trading_conditions()
        
        # 텔레그램 알림
        self.telegram.notify_start()
        
        # 텔레그램 명령어 수신 시작
        if self.telegram.enable_commands:
            self.telegram.start_listening(self._handle_telegram_command)
            self.logger.info("📱 텔레그램 명령어 수신 시작")
        
        print("✅ 트레이딩 시작됨")
    
    def _print_trading_conditions(self):
        """현재 매수 조건 출력"""
        print("\n" + "="*80)
        print("📋 현재 매수 조건")
        print("="*80)
        
        print("\n🎯 신호 점수제")
        if self.config['indicators'].get('use_signal_scoring', False):
            print(f"  ✅ 사용 중: 최소 {self.config['indicators']['min_signal_score']}점 필요")
            print(f"\n  📊 신호별 점수:")
            print(f"     거래량 폭증 (2배+)      : 3점")
            print(f"     MACD 골든크로스          : 3점")
            print(f"     RSI 강한 과매도 (<30)   : 3점")
            print(f"     거래량 급증 (1.8배)     : 2점")
            print(f"     RSI 약한 과매도 (30-35) : 2점")
            print(f"     BB 하단 반등             : 2점")
            print(f"     BB 하위 25%              : 2점")
            print(f"     MA5 상승                 : 1점")
        else:
            print(f"  ❌ 미사용: 신호 개수 기준 ({self.config['indicators']['min_signals_required']}개 이상)")
        
        print("\n📈 추세 확인")
        if self.config['indicators'].get('check_trend', False):
            print(f"  ✅ 사용 중: MA20 기울기 {self.config['indicators']['min_trend_strength']*100}% 이상")
            print(f"     → 횡보장 거래 금지")
        else:
            print(f"  ❌ 미사용")
        
        print("\n💰 투자 금액")
        if self.config['trading'].get('dynamic_allocation', False):
            print(f"  ✅ 동적 투자:")
            print(f"     기본 금액: {self.config['trading']['buy_amount_krw']:,}원")
            print(f"     최대 한도: {self.config['trading']['max_total_investment']:,}원")
            print(f"     점수 11점+: 기본 × 1.5배")
            print(f"     점수 9-10점: 기본 × 1.3배")
            print(f"     점수 7-8점: 기본 × 1.0배")
        else:
            print(f"  고정 금액: {self.config['trading']['buy_amount_krw']:,}원")
        
        print("\n🛡️ 안전 장치")
        print(f"  최대 스프레드: {self.config['trading'].get('max_spread_percent', 0.5)}%")
        print(f"  최소 호가잔량: {self.config['trading'].get('min_orderbook_depth_krw', 5000000):,}원")
        
        print("\n⏰ 거래 시간")
        if self.config['trading']['trading_hours'].get('enabled', False):
            sessions = self.config['trading']['trading_hours']['sessions']
            print(f"  ✅ 시간 필터 사용:")
            for session in sessions:
                print(f"     {session['start']:02d}:00 ~ {session['end']:02d}:00")
        else:
            print(f"  ❌ 24시간 거래")
        
        print("\n🎲 코인 선정")
        print(f"  최대 동시 거래: {self.config['trading']['max_coins']}개")
        print(f"  최소 거래량: {self.config['coin_selection']['min_volume_krw']/100000000:.0f}억원")
        print(f"  변동성 범위: {self.config['coin_selection']['min_volatility']}% ~ {self.config['coin_selection']['max_volatility']}%")
        
        excluded = self.config['coin_selection'].get('excluded_coins', [])
        if excluded:
            print(f"  제외 코인: {', '.join(excluded)}")
        
        print("="*80)
    
    def stop(self):
        """트레이딩 정지"""
        
        if not self.is_running:
            print("⚠️  실행 중이 아닙니다.")
            return
        
        self.logger.warning("⏹️  트레이딩 정지 요청")
        self.is_running = False
        
        # 모든 포지션 정리
        if self.stats.positions:
            self.logger.info("📤 보유 포지션 청산 중...")
            
            # 각 포지션별로 매도
            for coin in list(self.stats.positions.keys()):
                position = self.stats.positions[coin]
                
                # 포지션 수량만큼 매도
                sell_result = self.engine.execute_sell(coin, position, 1.0)
                
                if sell_result:
                    profit_krw = sell_result['total_krw'] - (position['buy_price'] * position['amount'])
                    self.stats.remove_position(coin, sell_result['price'], profit_krw, "정지시 청산")
        
        # 최종 잔고
        final_balance = self.engine.get_balance("KRW")
        self.stats.update_balance(final_balance)
        
        # 통계 저장
        self.logger.log_daily_stats(self.stats.get_current_status())
        
        # 텔레그램 알림
        total_profit = final_balance - self.stats.initial_balance
        self.telegram.notify_stop(final_balance, total_profit)
        
        # 텔레그램 명령어 수신 중지
        self.telegram.stop_listening()
        
        print("✅ 트레이딩 정지됨")
    
    def _handle_telegram_command(self, command):
        """텔레그램 명령어 처리"""
        
        try:
            cmd = command.strip().lower()
            
            # /status - 현재 상태
            if cmd == '/status' or cmd == '/상태':
                self._telegram_status()
            
            # /daily - 일일 통계
            elif cmd == '/daily' or cmd == '/일일':
                self._telegram_daily()
            
            # /positions - 포지션 현황
            elif cmd == '/positions' or cmd == '/포지션':
                self._telegram_positions()
            
            # /balance - 잔고
            elif cmd == '/balance' or cmd == '/잔고':
                self._telegram_balance()
            
            # /refresh - 종목 갱신
            elif cmd == '/refresh' or cmd == '/갱신':
                self._telegram_refresh()
            
            # /pause - 일시 정지
            elif cmd == '/pause' or cmd == '/정지':
                self._telegram_pause()
            
            # /resume - 재개
            elif cmd == '/resume' or cmd == '/재개':
                self._telegram_resume()
            
            # /help - 도움말
            elif cmd == '/help' or cmd == '/도움말':
                self._telegram_help()
            
            else:
                self.telegram.send_message(
                    f"❓ 알 수 없는 명령어: {command}\n"
                    f"/help 를 입력하여 사용 가능한 명령어를 확인하세요."
                )
        
        except Exception as e:
            self.telegram.send_message(f"⚠️ 명령어 처리 오류: {e}")
    
    def _telegram_status(self):
        """텔레그램: 상태 확인"""
        status = self.stats.get_current_status()
        
        # 사용 가능 금액 계산
        invested = sum(pos['buy_price'] * pos['amount'] for pos in self.stats.positions.values())
        available = min(self.max_total_investment - invested, status['current_balance'])
        
        state = "▶️ 실행 중" if self.is_running else "⏸️ 정지"
        if self.is_trading_paused:
            state += " (시간외)"
        if self.cooldown_until:
            state += " (쿨다운)"
        
        message = f"""📊 <b>현재 상태</b>

🔄 상태: {state}

💰 <b>자금 현황</b>
초기: {status['initial_balance']:,.0f}원
현재: {status['current_balance']:,.0f}원
투자중: {invested:,.0f}원
사용가능: {available:,.0f}원

📈 <b>수익</b>
총 평가액: {status['total_value']:,.0f}원
총 수익률: {status['total_return']:+.2f}%

📊 <b>거래 통계</b>
총 거래: {status['total_trades']}회
승률: {status['win_rate']:.1f}%
"""
        
        self.telegram.send_message(message)
    
    def _telegram_daily(self):
        """텔레그램: 일일 통계"""
        today = datetime.now().date()
        
        # 파일 + 메모리 통합
        file_trades = self.stats.load_daily_trades()
        memory_trades = [t for t in self.stats.trades if t['timestamp'].date() == today]
        
        all_trades_dict = {t['timestamp'].isoformat(): t for t in file_trades}
        for t in memory_trades:
            all_trades_dict[t['timestamp'].isoformat()] = t
        
        today_trades = list(all_trades_dict.values())
        
        if not today_trades:
            self.telegram.send_message("📅 오늘 거래 내역이 없습니다.")
            return
        
        wins = [t for t in today_trades if t['profit_krw'] > 0]
        losses = [t for t in today_trades if t['profit_krw'] <= 0]
        total_profit = sum(t['profit_krw'] for t in today_trades)
        
        message = f"""📅 <b>일일 통계</b>

날짜: {today.strftime('%Y-%m-%d')}

📊 거래: {len(today_trades)}회
✅ 승: {len(wins)}회
❌ 패: {len(losses)}회
📈 승률: {len(wins)/len(today_trades)*100:.1f}%

💰 총 손익: {total_profit:+,.0f}원
"""
        
        if wins:
            best = max(wins, key=lambda x: x['profit_krw'])
            message += f"\n🏆 최고: {best['coin'].replace('KRW-', '')} {best['profit_krw']:+,.0f}원"
        
        if losses:
            worst = min(losses, key=lambda x: x['profit_krw'])
            message += f"\n📉 최악: {worst['coin'].replace('KRW-', '')} {worst['profit_krw']:+,.0f}원"
        
        self.telegram.send_message(message)
    
    def _telegram_positions(self):
        """텔레그램: 포지션 현황"""
        if not self.stats.positions:
            self.telegram.send_message("📭 보유 포지션이 없습니다.")
            return
        
        message = "<b>📍 보유 포지션</b>\n\n"
        
        for ticker, pos in self.stats.positions.items():
            coin_name = ticker.replace('KRW-', '')
            current_price = self.engine.get_current_price(ticker)
            
            if current_price:
                profit_rate = ((current_price - pos['buy_price']) / pos['buy_price']) * 100
                profit_krw = (current_price - pos['buy_price']) * pos['amount']
                
                emoji = "💰" if profit_krw > 0 else "📉"
                
                message += f"""<b>{coin_name}</b>
매수: {pos['buy_price']:,.0f}원
현재: {current_price:,.0f}원
{emoji} 수익: {profit_rate:+.2f}% ({profit_krw:+,.0f}원)

"""
        
        self.telegram.send_message(message)
    
    def _telegram_balance(self):
        """텔레그램: 잔고 확인"""
        krw_balance = self.engine.get_balance("KRW")
        
        invested = sum(pos['buy_price'] * pos['amount'] for pos in self.stats.positions.values())
        total_value = krw_balance + invested
        
        message = f"""💰 <b>잔고</b>

원화: {krw_balance:,.0f}원
투자중: {invested:,.0f}원
총 평가액: {total_value:,.0f}원
"""
        
        self.telegram.send_message(message)
    
    def _telegram_refresh(self):
        """텔레그램: 종목 갱신"""
        if not self.is_running:
            self.telegram.send_message("⚠️ 프로그램이 실행 중이 아닙니다.")
            return
        
        # 현재 목록
        old_coins = set(self.target_coins)
        
        # 새 목록 선정
        new_coins = self.coin_selector.get_top_coins(self.max_coins)
        
        if not new_coins:
            self.telegram.send_message("❌ 종목 선정 실패")
            return
        
        new_coins_set = set(new_coins)
        
        # 변경사항
        added = new_coins_set - old_coins
        removed = old_coins - new_coins_set
        kept = old_coins & new_coins_set
        
        # 목록 업데이트
        self.target_coins = new_coins
        self.last_coin_refresh = datetime.now()
        
        message = f"""🔄 <b>종목 갱신 완료</b>

📊 변경사항
유지: {len(kept)}개
추가: {len(added)}개
제외: {len(removed)}개
"""
        
        if added:
            added_names = [c.replace('KRW-', '') for c in added]
            message += f"\n➕ 추가: {', '.join(added_names)}"
        
        if removed:
            removed_names = []
            for coin in removed:
                name = coin.replace('KRW-', '')
                if coin in self.stats.positions:
                    removed_names.append(f"{name} 📍")
                else:
                    removed_names.append(name)
            message += f"\n➖ 제외: {', '.join(removed_names)}"
        
        message += "\n\n💡 제외된 종목의 포지션은 유지됩니다"
        
        self.telegram.send_message(message)
        self.logger.info(f"텔레그램: 종목 갱신 - 유지 {len(kept)}, 추가 {len(added)}, 제외 {len(removed)}")
    
    def _telegram_pause(self):
        """텔레그램: 일시 정지"""
        if not self.is_running:
            self.telegram.send_message("⚠️ 이미 정지 상태입니다.")
            return
        
        self.is_trading_paused = True
        self.telegram.send_message("⏸️ 거래를 일시 정지했습니다.\n/resume 으로 재개할 수 있습니다.")
    
    def _telegram_resume(self):
        """텔레그램: 재개"""
        if not self.is_running:
            self.telegram.send_message("⚠️ 프로그램이 정지되어 있습니다.")
            return
        
        self.is_trading_paused = False
        self.cooldown_until = None
        self.telegram.send_message("▶️ 거래를 재개했습니다.")
    
    def _telegram_help(self):
        """텔레그램: 도움말"""
        message = """📱 <b>사용 가능한 명령어</b>

📊 <b>정보 조회</b>
/status - 현재 상태
/daily - 일일 통계
/positions - 보유 포지션
/balance - 잔고 확인

🎮 <b>제어</b>
/refresh - 종목 목록 갱신
/pause - 일시 정지
/resume - 거래 재개

❓ /help - 이 도움말
"""
        
        self.telegram.send_message(message)
    
    def status(self):
        """현재 상태 표시"""
        
        status = self.stats.get_current_status()
        
        print("\n" + "="*80)
        print("📊 현재 거래 상태")
        print("="*80)
        
        if not self.is_running:
            print("⏸️  상태: 정지")
        else:
            print("▶️  상태: 실행 중")
        
        print(f"\n💰 자금 현황")
        print(f"  초기 자금: {status['initial_balance']:,.0f}원")
        print(f"  현재 잔고: {status['current_balance']:,.0f}원")
        
        # 사용 가능 금액 계산
        invested_amount = sum(pos['buy_price'] * pos['amount'] for pos in self.stats.positions.values())
        available_investment = min(
            self.max_total_investment - invested_amount,
            status['current_balance']
        )
        
        print(f"  투자 중: {invested_amount:,.0f}원")
        print(f"  사용 가능: {available_investment:,.0f}원 (한도: {self.max_total_investment:,.0f}원)")
        print(f"  총 평가액: {status['total_value']:,.0f}원")
        print(f"  총 수익률: {status['total_return']:+.2f}%")
        print(f"  총 손익: {status['total_profit_krw']:+,.0f}원")
        
        print(f"\n📈 거래 통계")
        print(f"  총 거래 횟수: {status['total_trades']}회")
        print(f"  승/패: {status['wins']}승 {status['losses']}패")
        print(f"  승률: {status['win_rate']:.1f}%")
        print(f"  평균 손익: {status['avg_profit']:+,.0f}원")
        print(f"  최대 낙폭: {status['max_drawdown']:.2f}%")
        
        print(f"\n⏱️  운영 시간")
        if status['start_time']:
            print(f"  시작: {status['start_time']}")
            print(f"  경과: {status['trading_hours']:.1f}시간")
        
        # 보유 포지션
        if status['positions']:
            print(f"\n🎯 보유 포지션 ({len(status['positions'])}개)")
            for pos in status['positions']:
                coin_name = pos['coin'].replace('KRW-', '')
                holding_time = (datetime.now() - pos['buy_time']).total_seconds() / 60
                current_price = self.engine.get_balance(pos['coin'])
                
                print(f"  {coin_name}: 매수가 {pos['buy_price']:,.0f}원 | "
                      f"수량 {pos['amount']:.8f} | 보유시간 {holding_time:.0f}분")
        else:
            print(f"\n🎯 보유 포지션: 없음")
        
        # 거래 대상 코인
        if self.target_coins:
            print(f"\n🎲 거래 대상 코인")
            for coin in self.target_coins:
                coin_name = coin.replace('KRW-', '')
                print(f"  - {coin_name}")
        
        # 최근 거래
        recent_trades = self.stats.get_recent_trades(5)
        if recent_trades:
            print(f"\n📜 최근 거래 ({len(recent_trades)}건)")
            for trade in recent_trades:
                emoji = "📈" if trade['profit_krw'] > 0 else "📉"
                print(f"  {emoji} {trade['coin'].replace('KRW-', '')} | "
                      f"수익률 {trade['profit_rate']:+.2f}% | "
                      f"손익 {trade['profit_krw']:+,.0f}원 | "
                      f"{trade['reason']}")
        
        print("="*80 + "\n")
    
    def daily_stats(self):
        """일일 통계 표시 (파일 기록 포함)"""
        
        print("\n" + "="*80)
        print("📅 일일 거래 통계")
        print("="*80)
        
        # 오늘 날짜
        today = datetime.now().date()
        
        # 파일에서 오늘의 거래 로드
        file_trades = self.stats.load_daily_trades()
        
        # 메모리의 오늘 거래
        memory_trades = [t for t in self.stats.trades if t['timestamp'].date() == today]
        
        # 중복 제거 (timestamp 기준)
        all_trades_dict = {t['timestamp'].isoformat(): t for t in file_trades}
        for t in memory_trades:
            all_trades_dict[t['timestamp'].isoformat()] = t
        
        today_trades = list(all_trades_dict.values())
        
        if not today_trades:
            print("\n⚠️  오늘 거래 내역이 없습니다.")
            print("="*80 + "\n")
            return
        
        # 통계 계산
        total_trades = len(today_trades)
        wins = len([t for t in today_trades if t['profit_krw'] > 0])
        losses = len([t for t in today_trades if t['profit_krw'] <= 0])
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        total_profit = sum(t['profit_krw'] for t in today_trades)
        avg_profit = total_profit / total_trades if total_trades > 0 else 0
        
        best_trade = max(today_trades, key=lambda x: x['profit_rate'])
        worst_trade = min(today_trades, key=lambda x: x['profit_rate'])
        
        # 코인별 통계
        coin_profits = {}
        for trade in today_trades:
            coin = trade['coin'].replace('KRW-', '')
            if coin not in coin_profits:
                coin_profits[coin] = {'trades': 0, 'profit': 0}
            coin_profits[coin]['trades'] += 1
            coin_profits[coin]['profit'] += trade['profit_krw']
        
        # 출력
        print(f"\n📊 오늘 ({today.strftime('%Y-%m-%d')})")
        print(f"  총 거래: {total_trades}회")
        print(f"  승/패: {wins}승 {losses}패")
        print(f"  승률: {win_rate:.1f}%")
        
        print(f"\n💰 수익 현황")
        print(f"  총 손익: {total_profit:+,.0f}원")
        print(f"  평균 손익: {avg_profit:+,.0f}원")
        
        print(f"\n🏆 최고 거래")
        print(f"  코인: {best_trade['coin'].replace('KRW-', '')}")
        print(f"  수익률: {best_trade['profit_rate']:+.2f}%")
        print(f"  손익: {best_trade['profit_krw']:+,.0f}원")
        print(f"  사유: {best_trade['reason']}")
        
        print(f"\n📉 최악 거래")
        print(f"  코인: {worst_trade['coin'].replace('KRW-', '')}")
        print(f"  수익률: {worst_trade['profit_rate']:+.2f}%")
        print(f"  손익: {worst_trade['profit_krw']:+,.0f}원")
        print(f"  사유: {worst_trade['reason']}")
        
        print(f"\n📌 코인별 성과")
        sorted_coins = sorted(coin_profits.items(), 
                            key=lambda x: x[1]['profit'], 
                            reverse=True)
        
        for coin, stats in sorted_coins:
            emoji = "📈" if stats['profit'] > 0 else "📉"
            print(f"  {emoji} {coin}: {stats['trades']}회 | {stats['profit']:+,.0f}원")
        
        print("="*80 + "\n")
    
    def refresh_coins(self):
        """종목 목록 수동 갱신 (포지션 유지)"""
        
        if not self.is_running:
            print("⚠️  트레이딩이 실행 중이 아닙니다.")
            print("   'start' 명령어로 먼저 시작하세요.")
            return
        
        print("\n" + "="*80)
        print("🔄 종목 목록 갱신")
        print("="*80)
        
        # 현재 목록
        old_coins = set(self.target_coins)
        print(f"\n📋 현재 목록 ({len(old_coins)}개)")
        for coin in old_coins:
            in_position = "📍" if coin in self.stats.positions else "  "
            print(f"  {in_position} {coin.replace('KRW-', '')}")
        
        # 새 목록 선정
        self.logger.info("🔄 수동 종목 갱신 시작")
        new_coins = self.coin_selector.get_top_coins(self.max_coins)
        
        if not new_coins:
            print("\n❌ 새로운 종목 선정 실패")
            self.logger.warning("종목 갱신 실패")
            return
        
        new_coins_set = set(new_coins)
        
        # 변경사항 분석
        added = new_coins_set - old_coins
        removed = old_coins - new_coins_set
        kept = old_coins & new_coins_set
        
        print(f"\n📊 변경 사항")
        print(f"  유지: {len(kept)}개")
        print(f"  추가: {len(added)}개")
        print(f"  제외: {len(removed)}개")
        
        if added:
            print(f"\n➕ 추가된 종목")
            for coin in added:
                print(f"   {coin.replace('KRW-', '')}")
        
        if removed:
            print(f"\n➖ 제외된 종목")
            for coin in removed:
                has_position = "📍 포지션 유지" if coin in self.stats.positions else ""
                print(f"   {coin.replace('KRW-', '')} {has_position}")
        
        # 목록 업데이트
        self.target_coins = new_coins
        self.last_coin_refresh = datetime.now()
        
        print(f"\n✅ 종목 목록 갱신 완료")
        print(f"\n💡 안내:")
        print(f"   - 제외된 종목의 포지션은 유지됩니다")
        print(f"   - 매도 신호 발생 시 정상적으로 청산됩니다")
        print(f"   - 새로운 매수는 갱신된 목록에서만 진행됩니다")
        print("="*80 + "\n")
        
        self.logger.info(f"종목 갱신 완료: 유지 {len(kept)}, 추가 {len(added)}, 제외 {len(removed)}")
    
    def exit_program(self):
        """프로그램 종료"""
        
        if self.is_running:
            print("⚠️  먼저 트레이딩을 정지합니다.")
            self.stop()
            time.sleep(2)
        
        # 최종 통계 저장
        if self.stats.total_trades > 0:
            stats_data = self.stats.export_stats()
            stats_file = f"final_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            
            with open(stats_file, 'w', encoding='utf-8') as f:
                json.dump(stats_data, f, ensure_ascii=False, indent=2)
            
            self.logger.info(f"📁 최종 통계 저장: {stats_file}")
        
        self.logger.info("👋 프로그램 종료")
        print("\n✅ 프로그램이 종료되었습니다.")
        sys.exit(0)
    
    def _is_trading_hours(self):
        """현재 거래 시간인지 확인"""
        
        if not self.trading_hours_enabled:
            return True
        
        current_hour = datetime.now().hour
        
        for session in self.trading_sessions:
            start = session['start']
            end = session['end']
            
            if start <= current_hour < end:
                return True
        
        return False
    
    def _calculate_dynamic_investment(self, signal_score):
        """신호 강도에 따른 동적 투자 금액 계산"""
        
        if not self.dynamic_allocation:
            return self.buy_amount_krw
        
        # 현재 투자 중인 금액 계산
        current_investment = sum(
            pos['buy_price'] * pos['amount'] 
            for pos in self.stats.positions.values()
        )
        
        # 남은 투자 가능 금액
        available = self.max_total_investment - current_investment
        
        if available < self.config['trading']['min_trade_amount']:
            return 0
        
        # 신호 점수에 따른 투자 비율
        # 점수 7-8: 기본 금액
        # 점수 9-10: 1.3배
        # 점수 11+: 1.5배
        
        base_amount = self.buy_amount_krw
        
        if signal_score >= 11:
            multiplier = 1.5
        elif signal_score >= 9:
            multiplier = 1.3
        else:
            multiplier = 1.0
        
        investment = min(base_amount * multiplier, available)
        
        # 최소 금액 체크
        if investment < self.config['trading']['min_trade_amount']:
            return 0
        
        return investment
    
    def _refresh_coin_list(self):
        """코인 목록 갱신 및 이전 코인 정리"""
        
        self.logger.info("🔄 코인 목록 갱신 시작")
        
        # 새로운 코인 목록 가져오기
        new_coins = self.coin_selector.get_top_coins(self.max_coins)
        
        if not new_coins:
            self.logger.warning("⚠️  새로운 코인 선정 실패, 기존 목록 유지")
            return
        
        old_coins = set(self.target_coins)
        new_coins_set = set(new_coins)
        
        # 제외된 코인 찾기 (이전에는 있었는데 새 목록에는 없는 코인)
        removed_coins = old_coins - new_coins_set
        
        # 제외된 코인의 포지션 정리
        if removed_coins:
            self.logger.info(f"📤 목록에서 제외된 코인 정리: {', '.join([c.replace('KRW-', '') for c in removed_coins])}")
            
            for coin in removed_coins:
                if coin in self.stats.positions:
                    position = self.stats.positions[coin]
                    
                    # 포지션 청산
                    sell_result = self.engine.execute_sell(coin, position, 1.0)
                    
                    if sell_result:
                        profit_krw = sell_result['total_krw'] - (position['buy_price'] * position['amount'])
                        profit_rate = ((sell_result['price'] - position['buy_price']) / position['buy_price']) * 100
                        
                        self.stats.remove_position(coin, sell_result['price'], profit_krw, "목록갱신 정리")
                        
                        # 로그
                        self.logger.log_sell(
                            coin,
                            sell_result['price'],
                            sell_result['amount'],
                            sell_result['total_krw'],
                            sell_result['fee'],
                            profit_rate,
                            profit_krw,
                            "목록갱신 정리",
                            self.engine.get_balance("KRW")
                        )
        
        # 새로운 목록으로 교체
        self.target_coins = new_coins
        self.last_coin_refresh = datetime.now()
        
        self.logger.info(f"✅ 코인 목록 갱신 완료: {', '.join([c.replace('KRW-', '') for c in new_coins])}")
    
    def _trading_loop(self):
        """거래 루프 (별도 스레드에서 실행)"""
        
        self.logger.info("🔄 거래 루프 시작")
        
        while self.is_running:
            try:
                # 쿨다운 체크
                if self.cooldown_until:
                    if datetime.now() < self.cooldown_until:
                        remaining = (self.cooldown_until - datetime.now()).seconds // 60
                        if remaining % 5 == 0:  # 5분마다 로그
                            self.logger.info(f"❄️  쿨다운 중... 남은 시간: {remaining}분")
                        time.sleep(60)
                        continue
                    else:
                        self.logger.info("✅ 쿨다운 종료, 거래 재개")
                        self.cooldown_until = None
                
                # 일일 손실 제한 체크
                daily_profit, daily_trades = self.stats.get_daily_profit()
                daily_profit_pct = (daily_profit / self.stats.daily_start_balance * 100) if self.stats.daily_start_balance > 0 else 0
                
                if daily_profit_pct <= self.daily_loss_limit:
                    self.logger.warning(f"⛔ 일일 손실 제한 도달: {daily_profit_pct:.2f}%")
                    self.logger.warning(f"   {self.cooldown_minutes}분간 거래 중지")
                    self.cooldown_until = datetime.now() + timedelta(minutes=self.cooldown_minutes)
                    
                    # 텔레그램 알림
                    self.telegram.notify_cooldown(
                        f"일일 손실 {daily_profit_pct:.2f}% 도달",
                        self.cooldown_minutes
                    )
                    
                    continue
                
                # 거래 시간 체크
                if self.trading_hours_enabled:
                    is_trading_time = self._is_trading_hours()
                    
                    # 거래 시간이 아닐 때
                    if not is_trading_time and not self.is_trading_paused:
                        self.logger.info("⏸️  거래 시간 종료 - 일시 정지")
                        self.is_trading_paused = True
                        
                        # 다음 거래 시간 안내
                        current_hour = datetime.now().hour
                        next_session = None
                        for session in self.trading_sessions:
                            if session['start'] > current_hour:
                                next_session = session
                                break
                        
                        if next_session:
                            self.logger.info(f"  다음 거래 시간: {next_session['start']}시 ~ {next_session['end']}시")
                    
                    # 거래 시간이 다시 시작될 때
                    elif is_trading_time and self.is_trading_paused:
                        self.logger.info("▶️  거래 시간 시작 - 재개")
                        self.is_trading_paused = False
                        
                        # 코인 목록 갱신
                        self._refresh_coin_list()
                    
                    # 일시 정지 중이면 대기
                    if self.is_trading_paused:
                        time.sleep(60)  # 1분마다 체크
                        continue
                
                # 코인 목록 갱신 체크 (설정된 시간마다)
                if self.last_coin_refresh:
                    elapsed_hours = (datetime.now() - self.last_coin_refresh).total_seconds() / 3600
                    
                    if elapsed_hours >= self.refresh_interval_hours:
                        self._refresh_coin_list()
                
                # 각 코인별로 매매 체크
                for ticker in self.target_coins:
                    
                    # 포지션 없을 때 - 매수 검토
                    if ticker not in self.stats.positions:
                        
                        # 중복 매수 방지: 이미 매수 중인지 확인
                        with self.buy_lock:
                            if ticker in self.buying_in_progress:
                                self.logger.debug(f"  {ticker} 이미 매수 진행 중 - 건너뜀")
                                continue
                        
                        buy_signal, signals, current_price, signal_score = self.engine.check_buy_signal(ticker)
                        
                        if buy_signal and current_price:
                            # 호가창 안전성 체크
                            is_safe, safety_msg = self.engine.check_orderbook_safety(ticker)
                            if not is_safe:
                                self.logger.debug(f"  {ticker} 호가 불안정: {safety_msg}")
                                continue
                            
                            # 동적 투자 금액 계산
                            invest_amount = self._calculate_dynamic_investment(signal_score)
                            
                            if invest_amount >= self.config['trading']['min_trade_amount']:
                                # 잔고 확인
                                available_krw = self.engine.get_balance("KRW")
                                
                                if available_krw >= invest_amount:
                                    # 매수 시작 표시
                                    with self.buy_lock:
                                        self.buying_in_progress.add(ticker)
                                    
                                    try:
                                        # 매수 실행
                                        buy_result = self.engine.execute_buy(ticker, invest_amount)
                                        
                                        if buy_result:
                                            # 포지션 기록 (UUID 포함)
                                            self.stats.add_position(
                                                ticker,
                                                buy_result['price'],
                                                buy_result['amount'],
                                                buy_result.get('uuid')
                                            )
                                            
                                            # 잔고 업데이트
                                            new_balance = self.engine.get_balance("KRW")
                                            self.stats.update_balance(new_balance)
                                            
                                            # 로그 기록 (점수 포함)
                                            signal_str = f"{', '.join(signals)} (점수:{signal_score})"
                                            self.logger.info(f"🔵 매수 완료 | {ticker} | {invest_amount:,.0f}원 | {signal_str}")
                                            
                                            # 텔레그램 알림
                                            self.telegram.notify_buy(
                                                ticker,
                                                buy_result['price'],
                                                buy_result['amount'],
                                                invest_amount,
                                                signals,
                                                signal_score
                                            )
                                            
                                            self.logger.log_buy(
                                                ticker,
                                                buy_result['price'],
                                                buy_result['amount'],
                                                buy_result['total_krw'],
                                                buy_result['fee'],
                                                signals,
                                                new_balance
                                            )
                                    
                                    finally:
                                        # 매수 완료 (성공/실패 상관없이 제거)
                                        with self.buy_lock:
                                            self.buying_in_progress.discard(ticker)
                            else:
                                self.logger.debug(f"  {ticker} 투자 한도 초과 또는 부족")
                    
                    # 포지션 있을 때 - 매도 검토
                    elif ticker in self.stats.positions:
                        position = self.stats.positions[ticker]
                        
                        should_sell, reason, sell_ratio = self.engine.check_sell_signal(ticker, position)
                        
                        if should_sell:
                            # 매도 실행 (포지션 정보 전달)
                            sell_result = self.engine.execute_sell(ticker, position, sell_ratio)
                            
                            if sell_result:
                                # 수익 계산
                                buy_cost = position['buy_price'] * sell_result['amount']
                                profit_krw = sell_result['total_krw'] - buy_cost
                                profit_rate = ((sell_result['price'] - position['buy_price']) / position['buy_price']) * 100
                                
                                # 잔고 업데이트
                                new_balance = self.engine.get_balance("KRW")
                                self.stats.update_balance(new_balance)
                                
                                # 로그 기록
                                self.logger.log_sell(
                                    ticker,
                                    sell_result['price'],
                                    sell_result['amount'],
                                    sell_result['total_krw'],
                                    sell_result['fee'],
                                    profit_rate,
                                    profit_krw,
                                    reason,
                                    new_balance
                                )
                                
                                # 텔레그램 알림 (전량 매도 시에만)
                                if sell_ratio >= 1.0:
                                    holding_time = (datetime.now() - position['timestamp']).total_seconds()
                                    self.telegram.notify_sell(
                                        ticker,
                                        position['buy_price'],
                                        sell_result['price'],
                                        profit_rate,
                                        profit_krw,
                                        holding_time,
                                        reason
                                    )
                                
                                # 통계 업데이트
                                if sell_ratio >= 1.0:  # 전량 매도
                                    self.stats.remove_position(ticker, sell_result['price'], profit_krw, reason)
                                else:  # 분할 매도
                                    # 포지션 수량 감소
                                    position['amount'] -= sell_result['amount']
                                    
                                    # 남은 수량이 너무 작으면 전량 청산
                                    if position['amount'] * sell_result['price'] < 5000:
                                        self.logger.info(f"  잔여 수량 소액으로 전량 청산: {ticker}")
                                        final_sell = self.engine.execute_sell(ticker, position, 1.0)
                                        if final_sell:
                                            final_profit = final_sell['total_krw'] - (position['buy_price'] * position['amount'])
                                            self.stats.remove_position(ticker, final_sell['price'], final_profit, "소액청산")
                
                # 대기
                time.sleep(self.check_interval)
                
            except Exception as e:
                self.logger.log_error("거래 루프 오류", e)
                time.sleep(self.check_interval)
        
        self.logger.info("🔄 거래 루프 종료")


def print_help():
    """도움말 출력"""
    print("\n" + "="*80)
    print("📖 사용 가능한 명령어")
    print("="*80)
    print("  start   - 트레이딩 시작 (코인 선정 후 자동 매매 시작)")
    print("  stop    - 트레이딩 정지 (모든 포지션 청산)")
    print("  status  - 현재 거래 상태 및 통계 표시")
    print("  daily   - 오늘의 거래 통계 표시")
    print("  refresh - 종목 목록 갱신 (포지션은 유지)")
    print("  help    - 도움말 표시")
    print("  exit    - 프로그램 종료")
    print("")
    print("💡 Tip: 위/아래 방향키로 이전 명령어를 불러올 수 있습니다")
    print("="*80 + "\n")


def main():
    """메인 함수"""
    
    print("="*80)
    print("🤖 업비트 자동매매 봇 v1.0")
    print("="*80)
    print("설정 파일을 로드합니다...")
    
    try:
        bot = TradingBot('config.json')
    except FileNotFoundError:
        print("❌ config.json 파일을 찾을 수 없습니다.")
        return
    except Exception as e:
        print(f"❌ 초기화 오류: {e}")
        return
    
    print("✅ 봇 초기화 완료")
    print_help()
    
    # readline 설정 (명령어 히스토리)
    histfile = os.path.join(os.path.expanduser("~"), ".trading_bot_history")
    try:
        readline.read_history_file(histfile)
        readline.set_history_length(100)
    except FileNotFoundError:
        pass
    
    # 커맨드 루프
    while True:
        try:
            command = input("💻 명령어 입력 > ").strip().lower()
            
            # 히스토리 저장
            if command:
                try:
                    readline.write_history_file(histfile)
                except:
                    pass
            
            if command == 'start':
                bot.start()
            
            elif command == 'stop':
                bot.stop()
            
            elif command == 'status':
                bot.status()
            
            elif command == 'daily':
                bot.daily_stats()
            
            elif command == 'refresh':
                bot.refresh_coins()
            
            elif command == 'help':
                print_help()
            
            elif command == 'exit' or command == 'quit':
                bot.exit_program()
            
            elif command == '':
                continue
            
            else:
                print(f"⚠️  알 수 없는 명령어: {command}")
                print("'help'를 입력하여 사용 가능한 명령어를 확인하세요.")
        
        except KeyboardInterrupt:
            print("\n\n⚠️  Ctrl+C 감지됨. 종료하려면 'exit'를 입력하세요.")
        
        except Exception as e:
            print(f"❌ 오류 발생: {e}")


if __name__ == "__main__":
    main()
