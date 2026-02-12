"""
트레이딩 엔진 - 멀티 시그널 전략 실행
"""

import pyupbit
import pyupbit.request_api as request_api
import pandas as pd
import numpy as np
import time
import re
from datetime import datetime


class TradingEngine:
    def __init__(self, config, logger, stats):
        self.config = config
        self.logger = logger
        self.stats = stats
        
        self.upbit = None
        # Fee rate (fraction). Default 0.05% = 0.0005
        self.FEE = 0.0005
        try:
            fee_pct = config.get('trading', {}).get('fee_pct', None)
            if fee_pct is not None:
                self.FEE = float(fee_pct) / 100
        except Exception:
            self.FEE = 0.0005
        
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
        # ATR 기반 손절이 너무 타이트해지는 것을 방지 (예: minute1 ATR로 -0.3% 손절 과다 방지)
        # 값은 퍼센트(예: -0.7)로 설정하며, ATR 기반 손절이 이 값보다 덜(=더 타이트)하면 이 값으로 완화합니다.
        self.min_atr_stop_loss = None
        try:
            min_atr_sl_pct = config['risk_management'].get('min_atr_stop_loss_pct', None)
            if min_atr_sl_pct is not None:
                self.min_atr_stop_loss = float(min_atr_sl_pct) / 100
        except Exception:
            self.min_atr_stop_loss = None
        
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

        # RSI 진입 필터 (수익 관점에서 과매도 캐치 방지)
        try:
            self.rsi_buy_min = float(config.get('indicators', {}).get('rsi_buy_min', 50))
        except Exception:
            self.rsi_buy_min = 50.0
        try:
            self.rsi_buy_max = float(config.get('indicators', {}).get('rsi_buy_max', 70))
        except Exception:
            self.rsi_buy_max = 70.0

        # 매수 품질 필터(과매매/수수료 드래그 완화 목적)
        ind_cfg = config.get('indicators', {}) or {}
        val = ind_cfg.get('require_price_above_ma20', True)
        self.require_price_above_ma20 = True if val is None else bool(val)
        val = ind_cfg.get('require_strong_trigger', True)
        self.require_strong_trigger = True if val is None else bool(val)
        try:
            self.strong_trigger_min_volume_ratio = float(ind_cfg.get('strong_trigger_min_volume_ratio', 1.8))
        except Exception:
            self.strong_trigger_min_volume_ratio = 1.8
        
        # pyupbit Remaining-Req 파싱 오류 우회 패치
        self._patch_pyupbit_remaining_req_parser()
    
    def _patch_pyupbit_remaining_req_parser(self):
        """Remaining-Req 헤더 파싱 실패로 인한 예외를 완화"""
        try:
            # 이미 패치된 경우 중복 방지
            if getattr(request_api, "_patched_remaining_req_parser", False):
                return
            
            original_parse = request_api._parse
            
            def safe_parse(remaining_req):
                # 정상 케이스는 원래 파서 사용
                try:
                    return original_parse(remaining_req)
                except Exception:
                    pass
                
                # 변형 헤더 대응 (대소문자/공백/순서 유연 처리)
                text = str(remaining_req or "")
                group_match = re.search(r"group\s*=\s*([a-zA-Z\-]+)", text)
                min_match = re.search(r"min\s*=\s*([0-9]+)", text)
                sec_match = re.search(r"sec\s*=\s*([0-9]+)", text)
                
                return {
                    "group": group_match.group(1).lower() if group_match else "unknown",
                    "min": int(min_match.group(1)) if min_match else 0,
                    "sec": int(sec_match.group(1)) if sec_match else 0,
                }
            
            request_api._parse = safe_parse
            request_api._patched_remaining_req_parser = True
            self.logger.info("✅ pyupbit Remaining-Req 파서 안전 패치 적용")
        
        except Exception as e:
            self.logger.warning(f"⚠️ pyupbit 파서 패치 실패: {e}")
    
    def check_orderbook_safety(self, ticker):
        """호가창 안전성 체크 (스프레드, 호가잔량)"""
        try:
            orderbook = pyupbit.get_orderbook(ticker)
            if not orderbook or 'orderbook_units' not in orderbook:
                return False, "호가 정보 없음", {"ticker": ticker}
            
            units = orderbook['orderbook_units'][0]
            ask_price = units['ask_price']  # 매도 1호가
            bid_price = units['bid_price']  # 매수 1호가
            ask_size = units['ask_size']    # 매도 잔량
            bid_size = units['bid_size']    # 매수 잔량
            details = {
                "ticker": ticker,
                "ask_price": float(ask_price),
                "bid_price": float(bid_price),
                "ask_size": float(ask_size),
                "bid_size": float(bid_size),
            }
            
            # 스프레드 체크
            spread_pct = ((ask_price - bid_price) / bid_price) * 100
            details["spread_pct"] = float(spread_pct)
            if spread_pct > self.max_spread_pct:
                return False, f"스프레드 과다({spread_pct:.2f}%)", details
            
            # 호가 잔량 체크 (매수/매도 모두)
            bid_depth_krw = bid_price * bid_size
            ask_depth_krw = ask_price * ask_size
            details["bid_depth_krw"] = float(bid_depth_krw)
            details["ask_depth_krw"] = float(ask_depth_krw)
            
            if bid_depth_krw < self.min_orderbook_depth:
                return False, f"매수호가 부족({bid_depth_krw:,.0f}원)", details
            
            if ask_depth_krw < self.min_orderbook_depth:
                return False, f"매도호가 부족({ask_depth_krw:,.0f}원)", details
            
            return True, "안전", details
            
        except Exception as e:
            return False, f"호가 체크 오류: {e}", {"ticker": ticker, "error": f"{type(e).__name__}: {e}"}
    
    def connect(self, access_key, secret_key):
        """업비트 API 연결"""
        try:
            # 기본 키 형식 검증
            if not access_key or not secret_key:
                self.logger.error("업비트 API 연결 실패: access_key 또는 secret_key 누락")
                return False
            if access_key.startswith("YOUR_") or secret_key.startswith("YOUR_"):
                self.logger.error("업비트 API 연결 실패: 플레이스홀더 키가 설정되어 있습니다")
                return False
            if len(access_key) != 40 or len(secret_key) != 40:
                self.logger.warning(
                    f"업비트 API 키 길이 비정상 가능성: access({len(access_key)}), secret({len(secret_key)})"
                )

            self.upbit = pyupbit.Upbit(access_key, secret_key)
            
            # RemainingReqParsingError 등 일시 오류 우회용 재시도
            last_error = None
            for attempt in range(1, 6):
                try:
                    balance = self.upbit.get_balance("KRW")
                    
                    if balance is None:
                        last_error = "KRW 잔고 조회 결과가 None"
                        time.sleep(0.7)
                        continue
                    
                    balance = float(balance)
                    self.logger.info(f"✅ 업비트 API 연결 성공 | 보유 현금: {balance:,.0f}원")
                    return True
                
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    self.logger.warning(f"API 연결 재시도 {attempt}/5 실패 - {last_error}")
                    time.sleep(0.7)
            
            # 마지막 진단
            diag = None
            try:
                diag = self.upbit.get_balances()
            except Exception as diag_e:
                diag = f"get_balances 예외: {type(diag_e).__name__}: {diag_e}"
            
            self.logger.error(
                "업비트 API 연결 실패: KRW 잔고 조회 실패. "
                f"last_error={last_error} | 진단 get_balances={diag}"
            )
            return False
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
                return False, ["데이터부족"], None, 0, {"blocked_by": ["데이터부족"]}
            
            df = self.calculate_indicators(df)
            
            # 확정 봉만 사용 (iloc[-2])
            current = df.iloc[-2]  # 마감된 직전 봉
            prev = df.iloc[-3]
            candle_ts = None
            try:
                candle_ts = str(getattr(current, "name", "") or "")
            except Exception:
                candle_ts = None

            # RSI 범위 필터: 최근 데이터 기준으로는 RSI<40(특히 <35) 구간 진입이 손익/승률 모두 악화되는 경향
            rsi_value = current.get('rsi')
            if pd.isna(rsi_value):
                self.logger.debug(f"  {ticker} ❌ RSI 데이터 없음")
                return False, ["RSI없음"], current['close'], 0, {"blocked_by": ["RSI없음"], "candle_ts": candle_ts}

            if rsi_value < self.rsi_buy_min or rsi_value >= self.rsi_buy_max:
                self.logger.debug(
                    f"  {ticker} ❌ RSI 범위 아님 ({rsi_value:.1f}, "
                    f"{self.rsi_buy_min:.0f}~{self.rsi_buy_max:.0f})"
                )
                return False, [f"RSI({rsi_value:.1f})"], current['close'], 0, {
                    "blocked_by": ["RSI필터"],
                    "rsi": float(rsi_value),
                    "rsi_buy_min": float(self.rsi_buy_min),
                    "rsi_buy_max": float(self.rsi_buy_max),
                    "candle_ts": candle_ts,
                }

            blocked_by = []
            meta = {
                "ticker": ticker,
                "interval": "minute1",
                "candle_ts": candle_ts,
                "close": float(current.get("close", 0) or 0),
                "prev_close": float(prev.get("close", 0) or 0),
                "rsi": float(rsi_value),
                "prev_rsi": float(prev.get("rsi", 0) or 0) if not pd.isna(prev.get("rsi")) else None,
                "ma5": float(current.get("ma5", 0) or 0) if not pd.isna(current.get("ma5")) else None,
                "ma20": float(current.get("ma20", 0) or 0) if not pd.isna(current.get("ma20")) else None,
                "bb_lower": float(current.get("bb_lower", 0) or 0) if not pd.isna(current.get("bb_lower")) else None,
                "bb_upper": float(current.get("bb_upper", 0) or 0) if not pd.isna(current.get("bb_upper")) else None,
                "macd": float(current.get("macd", 0) or 0) if not pd.isna(current.get("macd")) else None,
                "macd_signal": float(current.get("macd_signal", 0) or 0) if not pd.isna(current.get("macd_signal")) else None,
                "volume": float(current.get("volume", 0) or 0) if not pd.isna(current.get("volume")) else None,
                "volume_ma": float(current.get("volume_ma", 0) or 0) if not pd.isna(current.get("volume_ma")) else None,
                "filters": {
                    "rsi_buy_min": float(self.rsi_buy_min),
                    "rsi_buy_max": float(self.rsi_buy_max),
                    "require_price_above_ma20": bool(self.require_price_above_ma20),
                    "require_strong_trigger": bool(self.require_strong_trigger),
                    "strong_trigger_min_volume_ratio": float(self.strong_trigger_min_volume_ratio),
                },
            }
            
            # 추세 확인 (횡보장 필터링)
            if self.check_trend:
                ma20_current = current['ma20']
                ma20_old = df['ma20'].iloc[-20]
                
                if pd.isna(ma20_current) or pd.isna(ma20_old):
                    self.logger.debug(f"  {ticker} ❌ MA20 데이터 없음")
                    return False, ["MA20없음"], None, 0, {
                        **meta,
                        "blocked_by": ["MA20없음"],
                    }
                
                trend_slope = (ma20_current - ma20_old) / ma20_old
                
                # 추세가 너무 약하면 거래 안 함 (횡보장)
                if abs(trend_slope) < self.min_trend_strength:
                    self.logger.debug(f"  {ticker} ❌ 횡보장 (기울기 {trend_slope*100:.2f}% < {self.min_trend_strength*100}%)")
                    return False, [f"횡보장({trend_slope*100:.2f}%)"], None, 0, {
                        **meta,
                        "blocked_by": ["횡보장"],
                        "ma20_slope": float(trend_slope),
                    }
            
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
            
            # 신호 2: RSI 강도 (모멘텀 구간 선호)
            # - 기본 진입 필터(rsi_buy_min~rsi_buy_max)를 통과한 상태에서 점수만 부여
            if current['rsi'] < 60:
                signals.append(f"RSI양호({current['rsi']:.1f})")
                signal_details.append(f"✅ RSI양호(2점, {current['rsi']:.1f})")
                total_score += 2
            else:
                signals.append(f"RSI강세({current['rsi']:.1f})")
                signal_details.append(f"✅ RSI강세(1점, {current['rsi']:.1f})")
                total_score += 1
            
            # 신호 3: 거래량 급증 (3점 - 강함)
            volume_ratio = current['volume'] / current['volume_ma']
            meta["volume_ratio"] = float(volume_ratio) if not pd.isna(volume_ratio) else None
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
            macd_cross = prev['macd'] <= prev['macd_signal'] and current['macd'] > current['macd_signal']
            meta["macd_golden_cross"] = bool(macd_cross)
            if macd_cross:
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

            # 신호 5.5: 가격이 MA20 위 (2점) - 추세/모멘텀 필터
            price_above_ma20 = (not pd.isna(current['ma20'])) and current['close'] > current['ma20']
            meta["price_above_ma20"] = bool(price_above_ma20)
            if price_above_ma20:
                signals.append("가격>MA20")
                signal_details.append("✅ 가격>MA20(2점)")
                total_score += 2
            else:
                signal_details.append("❌ 가격>MA20(미충족)")
            
            # 신호 6: BB 하위 위치 (2점)
            if not pd.isna(current['bb_upper']) and not pd.isna(current['bb_lower']):
                bb_position = (current['close'] - current['bb_lower']) / (current['bb_upper'] - current['bb_lower'])
                if bb_position < 0.25:
                    signals.append(f"BB하위({bb_position*100:.0f}%)")
                    signal_details.append(f"✅ BB하위(2점, {bb_position*100:.0f}%)")
                    total_score += 2
                else:
                    signal_details.append(f"❌ BB하위(미충족, {bb_position*100:.0f}%)")
                meta["bb_position"] = float(bb_position)

            # 품질 필터 1: 가격이 MA20 위에 있어야만 진입 (칼날 잡기 방지)
            if self.require_price_above_ma20 and not price_above_ma20:
                blocked_by.append("가격<MA20")

            # 품질 필터 2: 강한 트리거(거래량 or MACD) 없으면 스킵 (과매매/수수료 드래그 완화)
            if self.require_strong_trigger:
                strong_volume = (volume_ratio is not None) and (volume_ratio >= self.strong_trigger_min_volume_ratio)
                if (not strong_volume) and (not macd_cross):
                    blocked_by.append("강한트리거없음")

            if blocked_by:
                meta["blocked_by"] = blocked_by
                self.logger.debug(f"  {ticker} ❌ 매수 품질 필터로 스킵: {', '.join(blocked_by)}")
                return False, signals, current['close'], total_score, meta
            
            # 로그 출력
            if len(signals) > 0 or total_score > 0:
                self.logger.debug(f"  {ticker} 신호 점수: {total_score}점")
                for detail in signal_details:
                    self.logger.debug(f"     {detail}")
            
            # 신호 점수제 사용 시
            if self.use_signal_scoring:
                if total_score >= self.min_signal_score:
                    self.logger.info(f"  {ticker} ✅ 매수 조건 충족! (점수: {total_score}점)")
                    meta["blocked_by"] = []
                    meta["score"] = int(total_score)
                    meta["signals"] = list(signals)
                    return True, signals, current['close'], total_score, meta
                else:
                    self.logger.debug(f"  {ticker} ❌ 점수 부족 ({total_score}점 < {self.min_signal_score}점)")
                    meta["blocked_by"] = ["점수부족"]
                    meta["score"] = int(total_score)
                    meta["signals"] = list(signals)
                    return False, signals, current['close'], total_score, meta
            
            # 기존 방식 (신호 개수)
            if len(signals) >= self.min_signals:
                self.logger.info(f"  {ticker} ✅ 매수 조건 충족! (신호: {len(signals)}개)")
                meta["blocked_by"] = []
                meta["score"] = int(total_score)
                meta["signals"] = list(signals)
                return True, signals, current['close'], total_score, meta
            else:
                self.logger.debug(f"  {ticker} ❌ 신호 부족 ({len(signals)}개 < {self.min_signals}개)")
            
            meta["blocked_by"] = ["신호부족"]
            meta["score"] = int(total_score)
            meta["signals"] = list(signals)
            return False, signals, current['close'], total_score, meta
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 매수 신호 확인 오류", e)
            return False, [], None, 0, {"blocked_by": ["예외"], "error": f"{type(e).__name__}: {e}"}
    
    def check_sell_signal(self, ticker, position):
        """매도 신호 확인"""
        
        try:
            df = pyupbit.get_ohlcv(ticker, interval="minute1", count=200)
            if df is None:
                return False, "HOLD", 1.0, {"blocked_by": ["데이터없음"]}
            
            df = self.calculate_indicators(df)
            # 확정 봉 기반 지표(노이즈로 인한 잦은 매도 방지)
            current = df.iloc[-2]
            prev = df.iloc[-3]

            current_price = pyupbit.get_current_price(ticker)
            if current_price is None:
                try:
                    current_price = float(df.iloc[-1].get("close", current.get("close", 0)) or current.get("close", 0))
                except Exception:
                    current_price = float(current.get("close", 0) or 0)

            buy_price = position['buy_price']
            highest_price = position['highest_price']
            current_atr = current['atr']
            
            # 최고가 업데이트
            if current_price > highest_price:
                highest_price = current_price
                self.stats.update_position_highest(ticker, highest_price)
            
            profit_rate = (current_price - buy_price) / buy_price

            meta = {
                "ticker": ticker,
                "interval": "minute1",
                "current_price": float(current_price),
                "indicator_close": float(current.get("close", 0) or 0),
                "buy_price": float(buy_price),
                "highest_price": float(highest_price),
                "profit_rate": float(profit_rate),
                "rsi": float(current.get("rsi", 0) or 0) if not pd.isna(current.get("rsi")) else None,
                "bb_lower": float(current.get("bb_lower", 0) or 0) if not pd.isna(current.get("bb_lower")) else None,
                "bb_upper": float(current.get("bb_upper", 0) or 0) if not pd.isna(current.get("bb_upper")) else None,
                "atr": float(current_atr) if not pd.isna(current_atr) else None,
                "sold_ratio": None,
                "reason": None,
            }
            
            # 이미 매도한 비율 계산
            original_amount = position.get('original_amount', position['amount'])
            current_amount = position['amount']
            sold_ratio = 1.0 - (current_amount / original_amount) if original_amount > 0 else 0
            meta["sold_ratio"] = float(sold_ratio)
            
            # ATR 기반 손절/익절 계산 (ATR 사용 시)
            if self.use_atr and not pd.isna(current_atr) and current_atr > 0:
                # 기본 ATR 손절(가격 기준)
                atr_stop_loss = buy_price - (current_atr * self.atr_sl_multiplier)
                
                # ATR 손절 하한(퍼센트) 적용: 너무 타이트한 손절은 완화
                if self.min_atr_stop_loss is not None and buy_price > 0:
                    atr_sl_rate = -((current_atr * self.atr_sl_multiplier) / buy_price)  # 음수
                    effective_sl_rate = min(atr_sl_rate, self.min_atr_stop_loss)  # 더 타이트하면(min_atr_stop_loss)로 완화
                    atr_stop_loss = buy_price * (1 + effective_sl_rate)
                atr_take_profit = buy_price + (current_atr * self.atr_tp_multiplier)
                meta["atr_stop_loss"] = float(atr_stop_loss)
                meta["atr_take_profit"] = float(atr_take_profit)
                
                # ATR 기반 손절 (가격 기준)
                if current_price <= atr_stop_loss:
                    atr_loss_pct = ((current_price - buy_price) / buy_price) * 100
                    reason = f"ATR손절({atr_loss_pct:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, 1.0, meta
                
                # ATR 기반 익절 (가격 기준)
                if current_price >= atr_take_profit and profit_rate > 0.01:
                    reason = f"ATR익절({profit_rate*100:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, 1.0, meta
            
            # 1. 고정 % 손절 (폴백)
            if profit_rate <= self.stop_loss:
                reason = f"손절({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, 1.0, meta
            
            # 2. BB 하단 추가 이탈
            if current_price < current['bb_lower'] * 0.995:
                reason = f"BB하단이탈({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, 1.0, meta
            
            # 3. 트레일링 스탑
            if profit_rate > self.trailing_activation:
                trailing_loss = (current_price - highest_price) / highest_price
                if trailing_loss <= -self.trailing_stop:
                    reason = f"트레일링스탑({profit_rate*100:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, 1.0, meta
            
            # 4. 분할 익절 1차 (아직 1차 익절을 안 했을 때만)
            if profit_rate >= self.take_profit_1 and sold_ratio < 0.1:
                reason = f"1차익절({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, self.config['risk_management']['take_profit_1_ratio'], meta
            
            # 5. 분할 익절 2차 (1차는 했고 2차는 안 했을 때만)
            if profit_rate >= self.take_profit_2 and sold_ratio >= 0.4 and sold_ratio < 0.7:
                reason = f"2차익절({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, self.config['risk_management']['take_profit_2_ratio'], meta
            
            # 6. BB 상단 도달
            if current_price >= current['bb_upper'] * 0.98 and profit_rate > 0.01:
                reason = f"BB상단({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, 1.0, meta
            
            # 7. RSI 과매수
            if current['rsi'] > 70 and profit_rate > 0.015:
                reason = f"RSI과매수({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, 1.0, meta
            
            return False, "HOLD", 1.0, meta
            
        except Exception as e:
            self.logger.log_error(f"{ticker} 매도 신호 확인 오류", e)
            return False, "ERROR", 1.0, {"blocked_by": ["예외"], "error": f"{type(e).__name__}: {e}"}
    
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
            full_liquidation = sell_ratio >= 0.999
            if full_liquidation:
                # 전량 매도는 가용 수량 전체를 주문하여 잔량 최소화
                sell_amount = round(actual_balance, 8)
            else:
                sell_amount = round(actual_balance * sell_ratio, 8)
            
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
                        order_uuid = result['uuid']
                        # 체결 대기
                        time.sleep(self.limit_wait_seconds)
                        
                        # 체결 확인
                        order_info = self.upbit.get_order(order_uuid)
                        
                        if order_info:
                            executed_volume = float(order_info.get('executed_volume', 0) or 0)
                            paid_fee = float(order_info.get('paid_fee', 0) or 0)
                            
                            # 체결 금액(원화)을 최대한 정확히 계산 (trades > executed_funds > 가격*수량 폴백)
                            gross_krw = 0.0
                            trades = order_info.get('trades')
                            if isinstance(trades, list) and trades:
                                for t in trades:
                                    try:
                                        gross_krw += float(t.get('price', 0)) * float(t.get('volume', 0))
                                    except Exception:
                                        continue
                            
                            if gross_krw <= 0:
                                try:
                                    gross_krw = float(order_info.get('executed_funds', 0) or 0)
                                except Exception:
                                    gross_krw = 0.0
                            
                            if gross_krw <= 0 and executed_volume > 0:
                                gross_krw = executed_volume * ask_price
                            
                            limit_fee = paid_fee if paid_fee > 0 else gross_krw * (self.FEE / 2)
                            limit_net = (gross_krw - paid_fee) if paid_fee > 0 else gross_krw * (1 - self.FEE / 2)
                            limit_avg_price = (gross_krw / executed_volume) if executed_volume > 0 else ask_price
                            
                            # 완전 체결
                            if order_info.get('state') == 'done':
                                self.logger.info(f"  ✅ 지정가 체결: {limit_avg_price:,.0f}원")
                                
                                remaining_balance = self.get_tradable_balance(ticker)
                                return {
                                    'price': limit_avg_price,
                                    'amount': executed_volume,
                                    'total_krw': limit_net,
                                    'fee': limit_fee,
                                    'remaining_amount': remaining_balance
                                }
                            
                            # 부분 체결
                            if executed_volume > 0:
                                self.logger.warning(
                                    f"  ⚠️  부분체결: {executed_volume:.8f} / {sell_amount:.8f}"
                                )
                                
                                # 남은 주문 취소
                                self.upbit.cancel_order(order_uuid)
                                time.sleep(0.3)
                                
                                remaining_balance = self.get_tradable_balance(ticker)
                                remaining_price = self.get_current_price(ticker) or current_price
                                remaining_value = remaining_balance * remaining_price if remaining_price else 0
                                min_trade = self.config['trading']['min_trade_amount']
                                
                                # 남은 수량이 최소 주문금액 미만이면 부분체결만으로 종료
                                if remaining_balance <= 0 or remaining_value < min_trade:
                                    return {
                                        'price': limit_avg_price,
                                        'amount': executed_volume,
                                        'total_krw': limit_net,
                                        'fee': limit_fee,
                                        'remaining_amount': remaining_balance
                                    }
                                
                                self.logger.info(f"  ↪️  남은 {remaining_balance:.8f} 시장가 처리")
                                market_result = self.upbit.sell_market_order(ticker, round(remaining_balance, 8))
                                if market_result and 'uuid' in market_result:
                                    time.sleep(0.5)
                                    market_info = self.upbit.get_order(market_result['uuid'])
                                    
                                    if market_info:
                                        market_volume = float(market_info.get('executed_volume', 0) or 0)
                                        market_paid_fee = float(market_info.get('paid_fee', 0) or 0)
                                        
                                        market_gross = 0.0
                                        trades = market_info.get('trades')
                                        if isinstance(trades, list) and trades:
                                            for t in trades:
                                                try:
                                                    market_gross += float(t.get('price', 0)) * float(t.get('volume', 0))
                                                except Exception:
                                                    continue
                                        
                                        if market_gross <= 0 and market_volume > 0:
                                            market_avg = float(market_info.get('avg_sell_price', 0) or 0)
                                            if market_avg == 0:
                                                market_avg = remaining_price or current_price
                                                self.logger.warning(
                                                    f"  ⚠️  avg_sell_price 없음, current_price 사용: {market_avg:,.0f}원"
                                                )
                                            market_gross = market_volume * market_avg
                                        
                                        market_fee = market_paid_fee if market_paid_fee > 0 else market_gross * self.FEE
                                        market_net = (market_gross - market_paid_fee) if market_paid_fee > 0 else market_gross * (1 - self.FEE)
                                        
                                        total_volume = executed_volume + market_volume
                                        total_gross = gross_krw + market_gross
                                        total_fee = limit_fee + market_fee
                                        total_net = limit_net + market_net
                                        avg_price = (total_gross / total_volume) if total_volume > 0 else current_price
                                        remaining_balance = self.get_tradable_balance(ticker)
                                        
                                        return {
                                            'price': avg_price,
                                            'amount': total_volume,
                                            'total_krw': total_net,
                                            'fee': total_fee,
                                            'remaining_amount': remaining_balance
                                        }
                                
                                # 시장가 처리 실패 시 부분체결만 반환
                                remaining_balance = self.get_tradable_balance(ticker)
                                return {
                                    'price': limit_avg_price,
                                    'amount': executed_volume,
                                    'total_krw': limit_net,
                                    'fee': limit_fee,
                                    'remaining_amount': remaining_balance
                                }
                            
                            # 미체결 - 주문 취소 후 시장가로 폴백
                            self.logger.debug(f"  ⚠️  지정가 미체결, 시장가로 전환")
                            self.upbit.cancel_order(order_uuid)
                            time.sleep(0.3)
            
            # 2단계: 시장가 주문 (폴백 또는 기본)
            result = self.upbit.sell_market_order(ticker, sell_amount)
            
            if result is None:
                self.logger.warning(f"⚠️  {ticker} 매도 주문 실패")
                return None
            
            time.sleep(0.5)

            # UUID로 체결 정보 조회 (정확한 체결가/수수료 반영)
            if 'uuid' in result:
                order_info = self.upbit.get_order(result['uuid'])
                if order_info:
                    executed_volume = float(order_info.get('executed_volume', 0))
                    avg_price = float(order_info.get('avg_sell_price', 0))
                    paid_fee = float(order_info.get('paid_fee', 0))

                    if executed_volume > 0:
                        if avg_price == 0:
                            avg_price = current_price
                            self.logger.warning(
                                f"  ⚠️  avg_sell_price 없음, current_price 사용: {avg_price:,.0f}원"
                            )
                        
                        gross_krw = executed_volume * avg_price
                        net_krw = gross_krw - paid_fee if paid_fee > 0 else gross_krw * (1 - self.FEE)
                        fee = paid_fee if paid_fee > 0 else gross_krw * self.FEE
                        remaining_balance = self.get_tradable_balance(ticker)
                        return {
                            'price': avg_price,
                            'amount': executed_volume,
                            'total_krw': net_krw,
                            'fee': fee,
                            'remaining_amount': remaining_balance
                        }
            
            # 체결 정보 조회 실패 시 폴백
            total_krw = sell_amount * current_price
            fee = total_krw * self.FEE
            remaining_balance = self.get_tradable_balance(ticker)
            return {
                'price': current_price,
                'amount': sell_amount,
                'total_krw': total_krw * (1 - self.FEE),
                'fee': fee,
                'remaining_amount': remaining_balance
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
