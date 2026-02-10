"""
트레이딩 엔진 - 멀티 시그널 전략 실행
"""

import pyupbit
import pandas as pd
import numpy as np
import time
from datetime import datetime


class TradingEngine:
    def __init__(self, config, logger, stats):
        self.config = config
        self.logger = logger
        self.stats = stats
        
        self.upbit = None
        self.FEE = 0.0005  # 업비트 수수료 0.05%
        
        # 설정값 로드
        self.bb_period = config['indicators']['bb_period']
        self.bb_std = config['indicators']['bb_std']
        self.rsi_period = config['indicators']['rsi_period']
        self.min_signals = config['indicators']['min_signals_required']
        
        # 신호 점수제
        self.use_signal_scoring = config['indicators'].get('use_signal_scoring', False)
        self.min_signal_score = config['indicators'].get('min_signal_score', 7)
        
        # 추세 확인
        self.check_trend = config['indicators'].get('check_trend', False)
        self.min_trend_strength = config['indicators'].get('min_trend_strength', 0.02)
        
        # ATR 설정
        self.use_atr = config['risk_management'].get('use_atr', False)
        self.atr_period = config['risk_management'].get('atr_period', 14)
        self.atr_sl_multiplier = config['risk_management'].get('atr_stop_loss_multiplier', 1.5)
        self.atr_tp_multiplier = config['risk_management'].get('atr_take_profit_multiplier', 2.5)
        
        # 고정 % 손익
        self.stop_loss = config['risk_management']['stop_loss_pct'] / 100
        self.take_profit_1 = config['risk_management']['take_profit_1_pct'] / 100
        self.take_profit_2 = config['risk_management']['take_profit_2_pct'] / 100
        self.trailing_stop = config['risk_management']['trailing_stop_pct'] / 100
        self.trailing_activation = config['risk_management']['trailing_activation_pct'] / 100
        
        # 주문 설정
        self.order_type = config['trading'].get('order_type', 'market')
        self.limit_wait_seconds = config['trading'].get('limit_order_wait_seconds', 3)
        
        # 안전 장치
        self.max_spread_pct = config['trading'].get('max_spread_percent', 0.5)
        self.min_orderbook_depth = config['trading'].get('min_orderbook_depth_krw', 5000000)
    
    def check_orderbook_safety(self, ticker):
        """호가창 안전성 체크 (스프레드, 호가잔량)"""
        try:
            orderbook = pyupbit.get_orderbook(ticker)
            if not orderbook or 'orderbook_units' not in orderbook:
                return False, "호가 정보 없음"
            
            units = orderbook['orderbook_units'][0]
            ask_price = units['ask_price']  # 매도 1호가
            bid_price = units['bid_price']  # 매수 1호가
            ask_size = units['ask_size']    # 매도 잔량
            bid_size = units['bid_size']    # 매수 잔량
            
            # 스프레드 체크
            spread_pct = ((ask_price - bid_price) / bid_price) * 100
            if spread_pct > self.max_spread_pct:
                return False, f"스프레드 과다({spread_pct:.2f}%)"
            
            # 호가 잔량 체크 (매수/매도 모두)
            bid_depth_krw = bid_price * bid_size
            ask_depth_krw = ask_price * ask_size
            
            if bid_depth_krw < self.min_orderbook_depth:
                return False, f"매수호가 부족({bid_depth_krw:,.0f}원)"
            
            if ask_depth_krw < self.min_orderbook_depth:
                return False, f"매도호가 부족({ask_depth_krw:,.0f}원)"
            
            return True, "안전"
            
        except Exception as e:
            return False, f"호가 체크 오류: {e}"
    
    def connect(self, access_key, secret_key):
        """업비트 API 연결"""
        try:
            self.upbit = pyupbit.Upbit(access_key, secret_key)
            balance = self.upbit.get_balance("KRW")
            self.logger.info(f"✅ 업비트 API 연결 성공 | 보유 현금: {balance:,.0f}원")
            return True
        except Exception as e:
            self.logger.log_error("업비트 API 연결 실패", e)
            return False
    
    def calculate_indicators(self, df):
        """기술 지표 계산"""
        
        # 볼린저밴드
        df['bb_middle'] = df['close'].rolling(self.bb_period).mean()
        df['bb_std'] = df['close'].rolling(self.bb_period).std()
        df['bb_upper'] = df['bb_middle'] + (df['bb_std'] * self.bb_std)
        df['bb_lower'] = df['bb_middle'] - (df['bb_std'] * self.bb_std)
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # MACD
        exp1 = df['close'].ewm(span=self.config['indicators']['macd_fast'], adjust=False).mean()
        exp2 = df['close'].ewm(span=self.config['indicators']['macd_slow'], adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['macd_signal'] = df['macd'].ewm(span=self.config['indicators']['macd_signal'], adjust=False).mean()
        
        # 거래량 이동평균
        df['volume_ma'] = df['volume'].rolling(20).mean()
        
        # 이동평균선
        df['ma5'] = df['close'].rolling(5).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        
        # ATR (Average True Range)
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        df['atr'] = true_range.rolling(self.atr_period).mean()
        
        return df
    
    def check_buy_signal(self, ticker):
        """매수 신호 확인 - 추세 확인 및 신호 점수제 (확정 봉 사용)"""
        
        try:
            df = pyupbit.get_ohlcv(ticker, interval="minute1", count=200)
            if df is None or len(df) < 50:
                self.logger.debug(f"  {ticker} 데이터 부족")
                return False, ["데이터부족"], None, 0
            
            df = self.calculate_indicators(df)
            
            # 확정 봉만 사용 (iloc[-2])
            current = df.iloc[-2]  # 마감된 직전 봉
            prev = df.iloc[-3]
            
            # 추세 확인 (횡보장 필터링)
            if self.check_trend:
                ma20_current = current['ma20']
                ma20_old = df['ma20'].iloc[-20]
                
                if pd.isna(ma20_current) or pd.isna(ma20_old):
                    self.logger.debug(f"  {ticker} ❌ MA20 데이터 없음")
                    return False, ["MA20없음"], None, 0
                
                trend_slope = (ma20_current - ma20_old) / ma20_old
                
                # 추세가 너무 약하면 거래 안 함 (횡보장)
                if abs(trend_slope) < self.min_trend_strength:
                    self.logger.debug(f"  {ticker} ❌ 횡보장 (기울기 {trend_slope*100:.2f}% < {self.min_trend_strength*100}%)")
                    return False, [f"횡보장({trend_slope*100:.2f}%)"], None, 0
            
            # 신호 수집 및 점수 계산
            signals = []
            signal_details = []  # 상세 로그용
            total_score = 0
            
            # 신호 1: 볼린저밴드 하단 반등 (2점)
            if prev['close'] <= prev['bb_lower'] and current['close'] > current['bb_lower']:
                signals.append("BB하단반등")
                signal_details.append("✅ BB하단반등(2점)")
                total_score += 2
            else:
                signal_details.append("❌ BB하단반등(미충족)")
            
            # 신호 2: RSI 과매도 (3점 - 강함)
            if current['rsi'] < 30:
                signals.append(f"RSI과매도({current['rsi']:.1f})")
                signal_details.append(f"✅ RSI강과매도(3점, {current['rsi']:.1f})")
                total_score += 3
            elif current['rsi'] < 35:
                signals.append(f"RSI약과매도({current['rsi']:.1f})")
                signal_details.append(f"✅ RSI약과매도(2점, {current['rsi']:.1f})")
                total_score += 2
            else:
                signal_details.append(f"❌ RSI과매도(미충족, {current['rsi']:.1f})")
            
            # 신호 3: 거래량 급증 (3점 - 강함)
            volume_ratio = current['volume'] / current['volume_ma']
            if volume_ratio > 2.0:
                signals.append("거래량폭증")
                signal_details.append(f"✅ 거래량폭증(3점, {volume_ratio:.1f}배)")
                total_score += 3
            elif volume_ratio > 1.8:
                signals.append("거래량급증")
                signal_details.append(f"✅ 거래량급증(2점, {volume_ratio:.1f}배)")
                total_score += 2
            else:
                signal_details.append(f"❌ 거래량급증(미충족, {volume_ratio:.1f}배)")
            
            # 신호 4: MACD 골든크로스 (3점 - 강함)
            if prev['macd'] <= prev['macd_signal'] and current['macd'] > current['macd_signal']:
                signals.append("MACD골든크로스")
                signal_details.append("✅ MACD골든크로스(3점)")
                total_score += 3
            else:
                signal_details.append("❌ MACD골든크로스(미충족)")
            
            # 신호 5: 단기 이평선 상승 (1점)
            if current['ma5'] > prev['ma5'] and current['close'] > current['ma5']:
                signals.append("MA5상승")
                signal_details.append("✅ MA5상승(1점)")
                total_score += 1
            else:
                signal_details.append("❌ MA5상승(미충족)")
            
            # 신호 6: BB 하위 위치 (2점)
            if not pd.isna(current['bb_upper']) and not pd.isna(current['bb_lower']):
                bb_position = (current['close'] - current['bb_lower']) / (current['bb_upper'] - current['bb_lower'])
                if bb_position < 0.25:
                    signals.append(f"BB하위({bb_position*100:.0f}%)")
                    signal_details.append(f"✅ BB하위(2점, {bb_position*100:.0f}%)")
                    total_score += 2
                else:
                    signal_details.append(f"❌ BB하위(미충족, {bb_position*100:.0f}%)")
            
            # 로그 출력
            if len(signals) > 0 or total_score > 0:
                self.logger.debug(f"  {ticker} 신호 점수: {total_score}점")
                for detail in signal_details:
                    self.logger.debug(f"     {detail}")
            
            # 신호 점수제 사용 시
            if self.use_signal_scoring:
                if total_score >= self.min_signal_score:
                    self.logger.info(f"  {ticker} ✅ 매수 조건 충족! (점수: {total_score}점)")
                    return True, signals, current['close'], total_score
                else:
                    self.logger.debug(f"  {ticker} ❌ 점수 부족 ({total_score}점 < {self.min_signal_score}점)")
                    return False, signals, current['close'], total_score
            
            # 기존 방식 (신호 개수)
            if len(signals) >= self.min_signals:
                self.logger.info(f"  {ticker} ✅ 매수 조건 충족! (신호: {len(signals)}개)")
                return True, signals, current['close'], total_score
            else:
                self.logger.debug(f"  {ticker} ❌ 신호 부족 ({len(signals)}개 < {self.min_signals}개)")
            
            return False, signals, current['close'], total_score
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 매수 신호 확인 오류", e)
            return False, [], None, 0
    
    def check_sell_signal(self, ticker, position):
        """매도 신호 확인"""
        
        try:
            df = pyupbit.get_ohlcv(ticker, interval="minute1", count=200)
            if df is None:
                return False, "HOLD", 1.0
            
            df = self.calculate_indicators(df)
            current = df.iloc[-1]
            
            current_price = current['close']
            buy_price = position['buy_price']
            highest_price = position['highest_price']
            current_atr = current['atr']
            
            # 최고가 업데이트
            if current_price > highest_price:
                highest_price = current_price
                self.stats.update_position_highest(ticker, highest_price)
            
            profit_rate = (current_price - buy_price) / buy_price
            
            # 이미 매도한 비율 계산
            original_amount = position.get('original_amount', position['amount'])
            current_amount = position['amount']
            sold_ratio = 1.0 - (current_amount / original_amount) if original_amount > 0 else 0
            
            # ATR 기반 손절/익절 계산 (ATR 사용 시)
            if self.use_atr and not pd.isna(current_atr) and current_atr > 0:
                atr_stop_loss = buy_price - (current_atr * self.atr_sl_multiplier)
                atr_take_profit = buy_price + (current_atr * self.atr_tp_multiplier)
                
                # ATR 기반 손절 (가격 기준)
                if current_price <= atr_stop_loss:
                    atr_loss_pct = ((current_price - buy_price) / buy_price) * 100
                    return True, f"ATR손절({atr_loss_pct:.2f}%)", 1.0
                
                # ATR 기반 익절 (가격 기준)
                if current_price >= atr_take_profit and profit_rate > 0.01:
                    return True, f"ATR익절({profit_rate*100:.2f}%)", 1.0
            
            # 1. 고정 % 손절 (폴백)
            if profit_rate <= self.stop_loss:
                return True, f"손절({profit_rate*100:.2f}%)", 1.0
            
            # 2. BB 하단 추가 이탈
            if current_price < current['bb_lower'] * 0.995:
                return True, f"BB하단이탈({profit_rate*100:.2f}%)", 1.0
            
            # 3. 트레일링 스탑
            if profit_rate > self.trailing_activation:
                trailing_loss = (current_price - highest_price) / highest_price
                if trailing_loss <= -self.trailing_stop:
                    return True, f"트레일링스탑({profit_rate*100:.2f}%)", 1.0
            
            # 4. 분할 익절 1차 (아직 1차 익절을 안 했을 때만)
            if profit_rate >= self.take_profit_1 and sold_ratio < 0.1:
                return True, f"1차익절({profit_rate*100:.2f}%)", \
                       self.config['risk_management']['take_profit_1_ratio']
            
            # 5. 분할 익절 2차 (1차는 했고 2차는 안 했을 때만)
            if profit_rate >= self.take_profit_2 and sold_ratio >= 0.4 and sold_ratio < 0.7:
                return True, f"2차익절({profit_rate*100:.2f}%)", \
                       self.config['risk_management']['take_profit_2_ratio']
            
            # 6. BB 상단 도달
            if current_price >= current['bb_upper'] * 0.98 and profit_rate > 0.01:
                return True, f"BB상단({profit_rate*100:.2f}%)", 1.0
            
            # 7. RSI 과매수
            if current['rsi'] > 70 and profit_rate > 0.015:
                return True, f"RSI과매수({profit_rate*100:.2f}%)", 1.0
            
            return False, "HOLD", 1.0
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 매도 신호 확인 오류", e)
            return False, "ERROR", 1.0
    
    def execute_buy(self, ticker, invest_amount):
        """매수 실행 - 지정가 우선, 부분체결 안전 처리"""
        
        try:
            current_price = pyupbit.get_current_price(ticker)
            if current_price is None:
                return None
            
            total_executed_volume = 0
            total_executed_value = 0
            total_fees = 0
            
            # 주문 방식 결정
            if self.order_type == 'limit_with_fallback':
                # 1단계: 지정가 주문 시도
                orderbook = pyupbit.get_orderbook(ticker)
                if orderbook and 'orderbook_units' in orderbook:
                    bid_price = orderbook['orderbook_units'][0]['bid_price']
                    buy_amount = invest_amount / bid_price
                    
                    self.logger.debug(f"  {ticker} 지정가 매수 시도: {bid_price:,.0f}원")
                    
                    # 지정가 주문
                    result = self.upbit.buy_limit_order(ticker, bid_price, buy_amount)
                    
                    if result and 'uuid' in result:
                        order_uuid = result['uuid']
                        
                        # 체결 대기
                        time.sleep(self.limit_wait_seconds)
                        
                        # 체결 확인
                        order_info = self.upbit.get_order(order_uuid)
                        
                        if order_info:
                            executed_volume = float(order_info.get('executed_volume', 0))
                            trades_count = int(order_info.get('trades_count', 0))
                            
                            # 완전 체결
                            if order_info['state'] == 'done':
                                avg_price = float(order_info.get('avg_buy_price', 0))
                                paid_fee = float(order_info.get('paid_fee', 0))
                                
                                # avg_buy_price가 없거나 0이면 bid_price 사용
                                if avg_price == 0:
                                    avg_price = bid_price
                                    self.logger.warning(f"  ⚠️  avg_buy_price 없음, bid_price 사용: {avg_price:,.0f}원")
                                
                                self.logger.info(f"  ✅ 지정가 완전체결: {avg_price:,.0f}원 × {executed_volume:.8f}")
                                
                                return {
                                    'price': avg_price,
                                    'amount': executed_volume,
                                    'total_krw': invest_amount,
                                    'fee': paid_fee,
                                    'uuid': order_uuid
                                }
                            
                            # 부분 체결
                            elif executed_volume > 0:
                                self.logger.warning(f"  ⚠️  부분체결: {executed_volume:.8f} / {buy_amount:.8f}")
                                
                                # 부분 체결된 금액 계산
                                executed_value = executed_volume * bid_price
                                remaining_value = invest_amount - executed_value
                                
                                # 주문 취소
                                self.upbit.cancel_order(order_uuid)
                                time.sleep(0.3)
                                
                                # 남은 금액이 최소 주문금액 이상이면 시장가로 처리
                                if remaining_value >= self.config['trading']['min_trade_amount']:
                                    self.logger.info(f"  ↪️  남은 {remaining_value:,.0f}원 시장가 처리")
                                    
                                    # 시장가로 남은 금액 매수
                                    market_result = self.upbit.buy_market_order(ticker, remaining_value)
                                    if market_result and 'uuid' in market_result:
                                        time.sleep(0.5)
                                        market_order = self.upbit.get_order(market_result['uuid'])
                                        
                                        if market_order:
                                            market_volume = float(market_order.get('executed_volume', 0))
                                            market_price = float(market_order.get('avg_buy_price', current_price))
                                            market_fee = float(market_order.get('paid_fee', 0))
                                            
                                            # 합산
                                            total_volume = executed_volume + market_volume
                                            total_fees = float(order_info.get('paid_fee', 0)) + market_fee
                                            avg_price = (executed_volume * bid_price + market_volume * market_price) / total_volume
                                            
                                            self.logger.info(f"  ✅ 부분+시장가 체결완료: 평단 {avg_price:,.0f}원")
                                            
                                            return {
                                                'price': avg_price,
                                                'amount': total_volume,
                                                'total_krw': invest_amount,
                                                'fee': total_fees,
                                                'uuid': order_uuid  # 첫 주문 UUID
                                            }
                                
                                # 남은 금액이 적으면 부분체결만으로 종료
                                else:
                                    avg_price = float(order_info.get('avg_buy_price', bid_price))
                                    paid_fee = float(order_info.get('paid_fee', 0))
                                    
                                    if avg_price == 0:
                                        avg_price = bid_price
                                    
                                    self.logger.info(f"  ✅ 부분체결로 종료: {avg_price:,.0f}원")
                                    
                                    return {
                                        'price': avg_price,
                                        'amount': executed_volume,
                                        'total_krw': executed_volume * avg_price,
                                        'fee': paid_fee,
                                        'uuid': order_uuid
                                    }
                            
                            # 미체결 - 주문 취소 후 시장가로 폴백
                            else:
                                self.logger.debug(f"  ⚠️  지정가 미체결, 시장가로 전환")
                                self.upbit.cancel_order(order_uuid)
                                time.sleep(0.3)
            
            # 2단계: 시장가 주문 (폴백 또는 기본)
            result = self.upbit.buy_market_order(ticker, invest_amount)
            
            if result is None:
                self.logger.warning(f"⚠️  {ticker} 매수 주문 실패")
                return None
            
            time.sleep(0.5)
            
            # UUID로 정확한 체결 정보 확인
            if 'uuid' in result:
                order_info = self.upbit.get_order(result['uuid'])
                if order_info:
                    executed_volume = float(order_info.get('executed_volume', 0))
                    avg_price = float(order_info.get('avg_buy_price', 0))
                    paid_fee = float(order_info.get('paid_fee', 0))
                    
                    # avg_buy_price가 없거나 0이면 current_price 사용
                    if avg_price == 0:
                        avg_price = current_price
                        self.logger.warning(f"  ⚠️  avg_buy_price 없음, current_price 사용: {avg_price:,.0f}원")
                    
                    return {
                        'price': avg_price,
                        'amount': executed_volume,
                        'total_krw': invest_amount,
                        'fee': paid_fee,
                        'uuid': result['uuid']
                    }
            
            # UUID가 없으면 잔고 기반 (폴백)
            coin_balance = self.upbit.get_balance(ticker)
            avg_buy_price = self.upbit.get_avg_buy_price(ticker)
            
            if coin_balance <= 0:
                self.logger.warning(f"⚠️  {ticker} 매수 후 잔고 확인 실패")
                return None
            
            fee = invest_amount * self.FEE
            
            return {
                'price': avg_buy_price,
                'amount': coin_balance,
                'total_krw': invest_amount,
                'fee': fee,
                'uuid': result.get('uuid')
            }
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 매수 실행 오류", e)
            return None
    
    def execute_sell(self, ticker, position, sell_ratio=1.0):
        """매도 실행 - 실제 잔고 기준 (locked 제외)"""
        
        try:
            # 실제 거래 가능 수량 확인 (locked 제외)
            actual_balance = self.get_tradable_balance(ticker)
            
            if actual_balance <= 0:
                self.logger.warning(f"⚠️  {ticker} 매도 가능 수량 없음")
                return None
            
            # 포지션 수량과 비교
            position_amount = position['amount']
            
            # 5% 이상 차이나면 경고 및 업데이트
            if abs(actual_balance - position_amount) / max(position_amount, 0.00000001) > 0.05:
                diff_pct = abs(actual_balance - position_amount) / position_amount * 100
                self.logger.warning(
                    f"⚠️  {ticker} 수량 불일치: "
                    f"포지션 {position_amount:.8f} vs 실제 {actual_balance:.8f} "
                    f"({diff_pct:.1f}% 차이)"
                )
                # 실제 잔고로 포지션 업데이트
                position['amount'] = actual_balance
            
            # 실제 잔고 기준으로 매도 수량 계산
            sell_amount = actual_balance * sell_ratio
            
            # 소수점 정밀도 조정 (99.95% 사용으로 수수료 여유 확보)
            sell_amount = round(sell_amount * 0.9995, 8)
            
            if sell_amount <= 0:
                self.logger.warning(f"⚠️  {ticker} 매도 수량 계산 오류")
                return None
            
            current_price = pyupbit.get_current_price(ticker)
            if current_price is None:
                return None
            
            # 최소 주문 금액 체크 (5,500원)
            sell_value = sell_amount * current_price
            if sell_value < 5500:
                self.logger.warning(
                    f"⚠️  {ticker} 매도 금액 부족: {sell_value:,.0f}원 < 5,500원"
                )
                return None
            
            self.logger.info(
                f"  💰 매도 준비: {ticker} | "
                f"수량 {sell_amount:.8f} ({sell_ratio*100:.0f}%) | "
                f"예상금액 {sell_value:,.0f}원"
            )
            
            # 주문 방식 결정
            if self.order_type == 'limit_with_fallback':
                # 1단계: 지정가 주문 시도
                orderbook = pyupbit.get_orderbook(ticker)
                if orderbook and 'orderbook_units' in orderbook:
                    # 매도 1호가 (최선 매도가)
                    ask_price = orderbook['orderbook_units'][0]['ask_price']
                    
                    self.logger.debug(f"  {ticker} 지정가 매도 시도: {ask_price:,.0f}원")
                    
                    # 지정가 주문
                    result = self.upbit.sell_limit_order(ticker, ask_price, sell_amount)
                    
                    if result and 'uuid' in result:
                        # 체결 대기
                        time.sleep(self.limit_wait_seconds)
                        
                        # 체결 확인
                        order_info = self.upbit.get_order(result['uuid'])
                        
                        if order_info and order_info['state'] == 'done':
                            # 체결 완료
                            total_krw = sell_amount * ask_price
                            fee = total_krw * self.FEE / 2  # 지정가는 수수료 절반
                            
                            self.logger.info(f"  ✅ 지정가 체결: {ask_price:,.0f}원")
                            
                            return {
                                'price': ask_price,
                                'amount': sell_amount,
                                'total_krw': total_krw * (1 - self.FEE / 2),
                                'fee': fee
                            }
                        
                        else:
                            # 미체결 - 주문 취소 후 시장가로 폴백
                            self.logger.debug(f"  ⚠️  지정가 미체결, 시장가로 전환")
                            self.upbit.cancel_order(result['uuid'])
                            time.sleep(0.3)
            
            # 2단계: 시장가 주문 (폴백 또는 기본)
            result = self.upbit.sell_market_order(ticker, sell_amount)
            
            if result is None:
                self.logger.warning(f"⚠️  {ticker} 매도 주문 실패")
                return None
            
            time.sleep(0.5)
            
            total_krw = sell_amount * current_price
            fee = total_krw * self.FEE
            
            return {
                'price': current_price,
                'amount': sell_amount,
                'total_krw': total_krw * (1 - self.FEE),
                'fee': fee
            }
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 매도 실행 오류", e)
            return None
    
    def get_balance(self, currency="KRW"):
        """잔고 조회"""
        try:
            return self.upbit.get_balance(currency)
        except Exception as e:
            self.logger.log_error("잔고 조회 오류", e)
            return 0
    
    def get_current_price(self, ticker):
        """현재가 조회"""
        try:
            return pyupbit.get_current_price(ticker)
        except Exception as e:
            self.logger.log_error(f"{ticker} 현재가 조회 오류", e)
            return None
    
    def get_tradable_balance(self, ticker):
        """
        거래 가능한 실제 수량 조회 (locked 제외)
        
        Args:
            ticker: 코인 티커 (예: 'KRW-BTC')
        
        Returns:
            float: 매도 가능한 실제 수량 (locked 제외)
        """
        try:
            coin = ticker.split('-')[1]
            balances = self.upbit.get_balances()
            
            if not balances:
                self.logger.warning(f"⚠️  {ticker} 잔고 조회 실패")
                return 0
            
            for balance in balances:
                if balance['currency'] == coin:
                    total_balance = float(balance['balance'])
                    locked_balance = float(balance['locked'])
                    available = total_balance - locked_balance
                    
                    self.logger.debug(
                        f"📊 {coin} 잔고 | 총:{total_balance:.8f} | "
                        f"Locked:{locked_balance:.8f} | 가능:{available:.8f}"
                    )
                    
                    return max(0, available)
            
            return 0
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 잔고 조회 오류", e)
            return 0
    
    def emergency_sell_all(self):
        """긴급 전량 매도"""
        
        self.logger.warning("🚨 긴급 전량 매도 시작")
        
        try:
            balances = self.upbit.get_balances()
            
            for balance in balances:
                currency = balance['currency']
                
                if currency == 'KRW':
                    continue
                
                ticker = f"KRW-{currency}"
                amount = float(balance['balance'])
                
                if amount > 0:
                    self.logger.info(f"  매도 중: {ticker} ({amount})")
                    self.upbit.sell_market_order(ticker, amount)
                    time.sleep(0.3)
            
            self.logger.info("✅ 긴급 매도 완료")
            return True
            
        except Exception as e:
            self.logger.log_error("긴급 매도 오류", e)
            return False
