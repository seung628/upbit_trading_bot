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

        # 전략 설정: 비용 민감 추세 돌파 (기존 룰 사실상 초기화)
        strategy_cfg = config.get('strategy', {}) or {}
        self.strategy_mode = str(strategy_cfg.get('mode', 'trend_breakout')).lower()
        self.entry_interval = str(strategy_cfg.get('entry_interval', 'minute1'))
        self.htf_interval = str(strategy_cfg.get('htf_interval', 'minute15'))
        try:
            self.entry_breakout_lookback = int(strategy_cfg.get('entry_breakout_lookback', 20))
        except Exception:
            self.entry_breakout_lookback = 20
        try:
            self.entry_breakout_buffer = float(strategy_cfg.get('entry_breakout_buffer_pct', 0.05)) / 100
        except Exception:
            self.entry_breakout_buffer = 0.0005
        try:
            self.entry_volume_ratio_min = float(strategy_cfg.get('entry_volume_ratio_min', 1.6))
        except Exception:
            self.entry_volume_ratio_min = 1.6
        try:
            self.entry_rsi_min = float(strategy_cfg.get('entry_rsi_min', 52))
        except Exception:
            self.entry_rsi_min = 52.0
        try:
            self.entry_rsi_max = float(strategy_cfg.get('entry_rsi_max', 72))
        except Exception:
            self.entry_rsi_max = 72.0
        try:
            self.entry_ma_fast = int(strategy_cfg.get('entry_ma_fast', 20))
        except Exception:
            self.entry_ma_fast = 20
        try:
            self.entry_ma_slow = int(strategy_cfg.get('entry_ma_slow', 60))
        except Exception:
            self.entry_ma_slow = 60
        try:
            self.htf_ma_fast = int(strategy_cfg.get('htf_ma_fast', 20))
        except Exception:
            self.htf_ma_fast = 20
        try:
            self.htf_ma_slow = int(strategy_cfg.get('htf_ma_slow', 50))
        except Exception:
            self.htf_ma_slow = 50
        try:
            self.entry_min_score = int(strategy_cfg.get('entry_min_score', 8))
        except Exception:
            self.entry_min_score = 8

        # 매도 관리
        rm_cfg = config.get('risk_management', {}) or {}
        try:
            self.min_hold_minutes = int(rm_cfg.get('min_hold_minutes', 20))
        except Exception:
            self.min_hold_minutes = 20
        try:
            self.max_hold_minutes = int(rm_cfg.get('max_hold_minutes', 360))
        except Exception:
            self.max_hold_minutes = 360
        val = rm_cfg.get('use_partial_take_profit', False)
        self.use_partial_take_profit = False if val is None else bool(val)

        # OHLCV 캐시: 과도한 API 호출/요청 제한 완화
        self._ohlcv_cache = {}
        
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

    def _get_cached_ohlcv(self, ticker, interval="minute1", count=200, ttl_seconds=2):
        """OHLCV 조회 with 단기 캐시 (요청 수 제한 완화)."""
        now = time.time()
        key = (ticker, interval, int(count))

        if ttl_seconds and key in self._ohlcv_cache:
            ts, cached_df = self._ohlcv_cache[key]
            if (now - ts) < ttl_seconds and cached_df is not None:
                return cached_df.copy()

        df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
        if df is not None:
            self._ohlcv_cache[key] = (now, df.copy())
        return df
    
    def check_buy_signal(self, ticker):
        """매수 신호 확인 - 비용 민감 추세 돌파 전략."""

        try:
            base_count = max(260, self.entry_ma_slow + 60, self.entry_breakout_lookback + 60)
            df = self._get_cached_ohlcv(
                ticker,
                interval=self.entry_interval,
                count=base_count,
                ttl_seconds=2,
            )
            if df is None or len(df) < max(self.entry_ma_slow + 5, self.entry_breakout_lookback + 5, 80):
                self.logger.debug(f"  {ticker} 데이터 부족")
                return False, ["데이터부족"], None, 0, {"blocked_by": ["데이터부족"]}

            df = self.calculate_indicators(df)
            df['ema_fast'] = df['close'].ewm(span=self.entry_ma_fast, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=self.entry_ma_slow, adjust=False).mean()

            current = df.iloc[-2]  # 확정 봉
            prev = df.iloc[-3]
            candle_ts = str(getattr(current, "name", "") or "")

            if pd.isna(current.get('rsi')) or pd.isna(current.get('volume_ma')) or float(current.get('volume_ma', 0) or 0) <= 0:
                return False, ["지표부족"], float(current.get('close', 0) or 0), 0, {
                    "blocked_by": ["지표부족"],
                    "candle_ts": candle_ts,
                }

            price = float(current.get('close', 0) or 0)
            prev_price = float(prev.get('close', 0) or 0)
            rsi_value = float(current.get('rsi', 0) or 0)
            volume_ratio = float((current.get('volume', 0) or 0) / (current.get('volume_ma', 1) or 1))
            macd_cross = bool(prev['macd'] <= prev['macd_signal'] and current['macd'] > current['macd_signal'])

            breakout_window = df['high'].iloc[-(self.entry_breakout_lookback + 2):-2]
            if breakout_window is None or len(breakout_window) < self.entry_breakout_lookback:
                return False, ["돌파기준부족"], price, 0, {
                    "blocked_by": ["돌파기준부족"],
                    "candle_ts": candle_ts,
                }

            breakout_base = float(breakout_window.max())
            breakout_price = breakout_base * (1 + self.entry_breakout_buffer)

            trend_1m = bool(
                (price > float(current.get('ema_fast', 0) or 0) > float(current.get('ema_slow', 0) or 0))
                and (float(current.get('ema_fast', 0) or 0) >= float(prev.get('ema_fast', 0) or 0))
            )
            breakout_ok = bool(price > breakout_price)
            volume_ok = bool(volume_ratio >= self.entry_volume_ratio_min)
            rsi_ok = bool(self.entry_rsi_min <= rsi_value < self.entry_rsi_max)

            # 상위 타임프레임 추세 확인
            htf_count = max(140, self.htf_ma_slow + 60)
            htf_df = self._get_cached_ohlcv(
                ticker,
                interval=self.htf_interval,
                count=htf_count,
                ttl_seconds=20,
            )
            if htf_df is None or len(htf_df) < (self.htf_ma_slow + 5):
                return False, ["상위데이터부족"], price, 0, {
                    "blocked_by": ["상위데이터부족"],
                    "candle_ts": candle_ts,
                    "volume_ratio": volume_ratio,
                    "rsi": rsi_value,
                }

            htf_df['ema_fast'] = htf_df['close'].ewm(span=self.htf_ma_fast, adjust=False).mean()
            htf_df['ema_slow'] = htf_df['close'].ewm(span=self.htf_ma_slow, adjust=False).mean()
            htf_cur = htf_df.iloc[-2]
            htf_prev = htf_df.iloc[-3]

            htf_trend = bool(
                float(htf_cur.get('close', 0) or 0) > float(htf_cur.get('ema_fast', 0) or 0) > float(htf_cur.get('ema_slow', 0) or 0)
                and float(htf_cur.get('ema_fast', 0) or 0) >= float(htf_prev.get('ema_fast', 0) or 0)
            )

            signals = []
            blocked_by = []
            score = 0

            def add_block(reason):
                if reason not in blocked_by:
                    blocked_by.append(reason)

            if trend_1m:
                signals.append("1m추세상승")
                score += 2
            else:
                add_block("1m추세약세")

            if htf_trend:
                signals.append(f"{self.htf_interval}추세상승")
                score += 3
            else:
                add_block("상위추세약세")

            if breakout_ok:
                signals.append(f"{self.entry_breakout_lookback}봉돌파")
                score += 3
            else:
                add_block("돌파실패")

            if volume_ratio >= 2.0:
                signals.append("거래량폭증")
                score += 3
            elif volume_ok:
                signals.append("거래량증가")
                score += 2
            else:
                add_block("거래량부족")

            if rsi_ok:
                signals.append(f"RSI적정({rsi_value:.1f})")
                score += 1
            else:
                add_block("RSI범위이탈")

            price_above_ma20 = not pd.isna(current.get('ma20')) and price > float(current.get('ma20', 0) or 0)
            if self.require_price_above_ma20 and not price_above_ma20:
                add_block("가격<MA20")

            if self.require_strong_trigger and (not volume_ok) and (not macd_cross):
                add_block("강한트리거없음")

            meta = {
                "ticker": ticker,
                "strategy_mode": self.strategy_mode,
                "entry_interval": self.entry_interval,
                "htf_interval": self.htf_interval,
                "candle_ts": candle_ts,
                "close": price,
                "prev_close": prev_price,
                "rsi": rsi_value,
                "volume_ratio": float(volume_ratio),
                "macd_golden_cross": bool(macd_cross),
                "breakout_base": float(breakout_base),
                "breakout_price": float(breakout_price),
                "trend_1m": bool(trend_1m),
                "trend_htf": bool(htf_trend),
                "price_above_ma20": bool(price_above_ma20),
                "filters": {
                    "entry_breakout_lookback": int(self.entry_breakout_lookback),
                    "entry_breakout_buffer_pct": float(self.entry_breakout_buffer * 100),
                    "entry_volume_ratio_min": float(self.entry_volume_ratio_min),
                    "entry_rsi_min": float(self.entry_rsi_min),
                    "entry_rsi_max": float(self.entry_rsi_max),
                    "entry_ma_fast": int(self.entry_ma_fast),
                    "entry_ma_slow": int(self.entry_ma_slow),
                    "htf_ma_fast": int(self.htf_ma_fast),
                    "htf_ma_slow": int(self.htf_ma_slow),
                    "entry_min_score": int(self.entry_min_score),
                },
                "blocked_by": list(blocked_by),
                "signals": list(signals),
                "score": int(score),
            }

            if blocked_by:
                self.logger.debug(f"  {ticker} ❌ 매수 차단: {', '.join(blocked_by)}")
                return False, signals, price, score, meta

            if score < self.entry_min_score:
                meta["blocked_by"] = ["점수부족"]
                self.logger.debug(f"  {ticker} ❌ 점수 부족 ({score}점 < {self.entry_min_score}점)")
                return False, signals, price, score, meta

            self.logger.info(f"  {ticker} ✅ 매수 조건 충족! (점수: {score}점)")
            return True, signals, price, score, meta

        except Exception as e:
            self.logger.log_error(f"{ticker} 매수 신호 확인 오류", e)
            return False, [], None, 0, {"blocked_by": ["예외"], "error": f"{type(e).__name__}: {e}"}
    
    def check_sell_signal(self, ticker, position):
        """매도 신호 확인"""
        
        try:
            df = self._get_cached_ohlcv(ticker, interval="minute1", count=260, ttl_seconds=2)
            if df is None:
                return False, "HOLD", 1.0, {"blocked_by": ["데이터없음"]}
            
            df = self.calculate_indicators(df)
            df['ema_fast'] = df['close'].ewm(span=self.entry_ma_fast, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=self.entry_ma_slow, adjust=False).mean()

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
            hold_minutes = 0.0
            try:
                hold_minutes = (datetime.now() - position['timestamp']).total_seconds() / 60.0
            except Exception:
                hold_minutes = 0.0
            
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
                "hold_minutes": float(hold_minutes),
                "sold_ratio": None,
                "reason": None,
            }
            
            # 이미 매도한 비율 계산
            original_amount = position.get('original_amount', position['amount'])
            current_amount = position['amount']
            sold_ratio = 1.0 - (current_amount / original_amount) if original_amount > 0 else 0
            meta["sold_ratio"] = float(sold_ratio)

            # 손절 기준: 고정 손절 + ATR 손절 중 더 넓은(덜 타이트한) 값 사용
            effective_stop_rate = float(self.stop_loss)
            atr_stop_rate = None
            if self.use_atr and not pd.isna(current_atr) and current_atr > 0 and buy_price > 0:
                atr_stop_rate = -((current_atr * self.atr_sl_multiplier) / buy_price)
                if self.min_atr_stop_loss is not None:
                    atr_stop_rate = min(atr_stop_rate, self.min_atr_stop_loss)
                effective_stop_rate = min(float(self.stop_loss), float(atr_stop_rate))

            meta["effective_stop_rate"] = float(effective_stop_rate)
            if atr_stop_rate is not None:
                meta["atr_stop_rate"] = float(atr_stop_rate)

            if profit_rate <= effective_stop_rate:
                reason = f"손절({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, 1.0, meta

            # 트레일링: 수익 구간에서만 작동
            if profit_rate >= self.trailing_activation and highest_price > 0:
                trailing_drawdown = (current_price - highest_price) / highest_price
                meta["trailing_drawdown"] = float(trailing_drawdown)
                if trailing_drawdown <= -self.trailing_stop:
                    reason = f"트레일링({profit_rate*100:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, 1.0, meta

            # 최소 보유 시간 이전에는 소프트 청산 금지(과매매/수수료 드래그 억제)
            if hold_minutes < self.min_hold_minutes:
                meta["blocked_by"] = ["min_hold"]
                return False, "HOLD", 1.0, meta

            # 추세 이탈 청산 (1분 + 상위 타임프레임)
            ema_fast = float(current.get('ema_fast', 0) or 0)
            ema_slow = float(current.get('ema_slow', 0) or 0)
            trend_break_1m = bool(current_price < ema_fast and ema_fast < ema_slow)

            htf_break = False
            htf_df = self._get_cached_ohlcv(
                ticker,
                interval=self.htf_interval,
                count=max(140, self.htf_ma_slow + 60),
                ttl_seconds=20,
            )
            if htf_df is not None and len(htf_df) >= (self.htf_ma_slow + 5):
                htf_df['ema_fast'] = htf_df['close'].ewm(span=self.htf_ma_fast, adjust=False).mean()
                htf_df['ema_slow'] = htf_df['close'].ewm(span=self.htf_ma_slow, adjust=False).mean()
                htf_cur = htf_df.iloc[-2]
                htf_break = bool(
                    float(htf_cur.get('close', 0) or 0) < float(htf_cur.get('ema_fast', 0) or 0)
                    or float(htf_cur.get('ema_fast', 0) or 0) < float(htf_cur.get('ema_slow', 0) or 0)
                )

            rsi_break = False
            if not pd.isna(current.get('rsi')):
                rsi_break = bool(float(current.get('rsi', 0) or 0) < max(45.0, self.entry_rsi_min - 8.0))

            meta["trend_break_1m"] = bool(trend_break_1m)
            meta["trend_break_htf"] = bool(htf_break)
            meta["rsi_break"] = bool(rsi_break)

            if trend_break_1m and (htf_break or rsi_break):
                reason = f"추세이탈({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                return True, reason, 1.0, meta

            # 최대 보유 시간 도달 시 수익 보호 또는 약세 시 정리
            if self.max_hold_minutes > 0 and hold_minutes >= self.max_hold_minutes:
                if profit_rate > 0 or trend_break_1m:
                    reason = f"시간청산({hold_minutes:.0f}m,{profit_rate*100:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, 1.0, meta

            # 과열 익절 (분할 익절 기본 비활성)
            if self.use_partial_take_profit:
                if profit_rate >= self.take_profit_1 and sold_ratio < 0.1:
                    reason = f"1차익절({profit_rate*100:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, self.config['risk_management']['take_profit_1_ratio'], meta

                if profit_rate >= self.take_profit_2 and sold_ratio >= 0.4 and sold_ratio < 0.7:
                    reason = f"2차익절({profit_rate*100:.2f}%)"
                    meta["reason"] = reason
                    return True, reason, self.config['risk_management']['take_profit_2_ratio'], meta
            else:
                if (
                    profit_rate >= self.take_profit_2
                    and not pd.isna(current.get('rsi'))
                    and float(current.get('rsi', 0) or 0) >= 78
                ):
                    reason = f"과열익절({profit_rate*100:.2f}%)"
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
