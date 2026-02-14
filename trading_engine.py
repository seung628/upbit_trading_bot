"""
트레이딩 엔진 - 레짐 기반 전략 실행
"""

import pyupbit
import pyupbit.request_api as request_api
import pandas as pd
import time
import re
from datetime import datetime


class TradingEngine:
    def __init__(self, config, logger, stats):
        self.config = config
        self.logger = logger
        self.stats = stats
        self.upbit = None

        trading_cfg = config.get("trading", {}) or {}
        strategy_cfg = config.get("strategy", {}) or {}
        risk_cfg = config.get("risk_management", {}) or {}
        ind_cfg = config.get("indicators", {}) or {}

        self.FEE = 0.0005
        try:
            fee_pct = trading_cfg.get("fee_pct", None)
            if fee_pct is not None:
                self.FEE = float(fee_pct) / 100
        except Exception:
            self.FEE = 0.0005

        self.max_total_investment = float(trading_cfg.get("max_total_investment", 3000000))
        self.max_spread_pct = float(trading_cfg.get("max_spread_percent", 0.5))
        self.min_orderbook_depth = float(trading_cfg.get("min_orderbook_depth_krw", 5000000))
        self.order_type = trading_cfg.get("order_type", "market")
        self.limit_wait_seconds = int(trading_cfg.get("limit_order_wait_seconds", 3))

        self.rsi_period = int(ind_cfg.get("rsi_period", 14))
        self.bb_period = int(ind_cfg.get("bb_period", 20))
        self.bb_std = float(ind_cfg.get("bb_std", 2.0))
        self.atr_period = int(risk_cfg.get("atr_period", 14))

        self.entry_interval = str(strategy_cfg.get("entry_interval", "minute20"))
        self.signal_candle_minutes = int(strategy_cfg.get("signal_candle_minutes", 20))
        self.analysis_lookback = int(strategy_cfg.get("analysis_lookback", 240))

        self.stop_loss = float(risk_cfg.get("stop_loss_pct", -1.8)) / 100.0
        self.trailing_stop = float(risk_cfg.get("trailing_stop_pct", 1.0)) / 100.0
        self.trailing_activation = float(risk_cfg.get("trailing_activation_pct", 2.0)) / 100.0
        self.min_hold_minutes = int(risk_cfg.get("min_hold_minutes", 20))
        self.max_hold_minutes = int(risk_cfg.get("max_hold_minutes", 360))
        self.time_stop_candles = int(risk_cfg.get("time_stop_candles", 10))
        self.default_risk_per_trade_pct = self._parse_risk_pct(risk_cfg.get("risk_per_trade_pct", 0.4), 0.004)

        self.strategy_mode = str(strategy_cfg.get("mode", "regime_spec")).lower()
        self.regime_reference_ticker = self._normalize_ticker(strategy_cfg.get("regime_reference", "KRW-BTC"))
        self.regime_check_minutes = int(strategy_cfg.get("regime_check_minutes", 20))
        self.regime_confirm_count = int(strategy_cfg.get("regime_confirm_count", 3))
        self.regime_min_hold_minutes = int(strategy_cfg.get("regime_min_hold_minutes", 0))
        self.max_positions = int(strategy_cfg.get("max_positions", trading_cfg.get("max_coins", 2)))

        time_filter_cfg = strategy_cfg.get("entry_time_filter", {}) or {}
        self.entry_block_start_hour = int(time_filter_cfg.get("start_hour", 2))
        self.entry_block_end_hour = int(time_filter_cfg.get("end_hour", 6))
        self.volatility_tr_atr_max = float(strategy_cfg.get("volatility_tr_atr_max", 3.0))

        btc_filter_cfg = strategy_cfg.get("btc_filter", {}) or {}
        self.btc_filter_enabled = bool(btc_filter_cfg.get("enabled", True))
        self.btc_filter_ticker = self._normalize_ticker(
            btc_filter_cfg.get("ticker", self.regime_reference_ticker)
        )
        self.btc_filter_ema_period = int(btc_filter_cfg.get("ema_period", 50))

        self.sol_breakout_lookback = int(strategy_cfg.get("sol_breakout_lookback", 48))
        self.sol_retest_atr_tolerance = float(strategy_cfg.get("sol_retest_atr_tolerance", 0.2))
        self.sol_stop_atr = float(strategy_cfg.get("sol_stop_atr", 0.5))
        self.sol_partial_tp_r = float(strategy_cfg.get("sol_partial_tp_r", 1.2))
        self.sol_trailing_activate_r = float(strategy_cfg.get("sol_trailing_activate_r", 2.2))
        self.sol_trailing_stop_pct = float(strategy_cfg.get("sol_trailing_stop_pct", 1.2)) / 100.0

        self.doge_volume_spike_min = float(strategy_cfg.get("doge_volume_spike_min", 1.3))
        self.doge_rsi_min = float(strategy_cfg.get("doge_rsi_min", 55))
        self.doge_pullback_atr_tolerance = float(strategy_cfg.get("doge_pullback_atr_tolerance", 0.2))
        self.doge_stop_pct = float(strategy_cfg.get("doge_stop_pct", 0.8)) / 100.0
        self.doge_time_stop_candles = int(strategy_cfg.get("doge_time_stop_candles", 6))
        self.doge_target_r = float(strategy_cfg.get("doge_target_r", 1.0))

        self.ada_range_lookback = int(strategy_cfg.get("ada_range_lookback", 96))
        self.ada_entry_lower_pct = float(strategy_cfg.get("ada_entry_lower_pct", 0.15))
        self.ada_take_profit_upper_pct = float(strategy_cfg.get("ada_take_profit_upper_pct", 0.85))
        self.ada_rsi_max = float(strategy_cfg.get("ada_rsi_max", 28))
        self.ada_stop_pct = float(strategy_cfg.get("ada_stop_pct", 0.9)) / 100.0

        self.universe = self._build_universe(strategy_cfg)
        self.base_weight_caps = self._build_weight_caps(strategy_cfg)
        self.risk_per_symbol_pct = self._build_risk_per_symbol(strategy_cfg, risk_cfg)

        self.global_regime = "RANGE"
        self._regime_candidate = None
        self._regime_candidate_count = 0
        self._last_regime_check = None
        self._regime_changed_at = None

        self._ohlcv_cache = {}
        self._last_resample_closed_ts = {}
        self._last_log_bucket = {}
        self._last_btc_filter_signature = None

        self._patch_pyupbit_remaining_req_parser()

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    def _throttled_info(self, key, message, bucket_seconds=60):
        try:
            bucket = int(time.time() // max(1, int(bucket_seconds)))
        except Exception:
            bucket = int(time.time())
        if self._last_log_bucket.get(key) == bucket:
            return
        self._last_log_bucket[key] = bucket
        self.logger.info(message)

    def _safe_get_order(self, uuid):
        """주문 조회 실패 시 None 반환 (예외를 상위 매수 플로우로 전파하지 않음)."""
        try:
            if self.upbit is None:
                return None
            return self.upbit.get_order(uuid)
        except Exception as e:
            self.logger.warning(f"GET_ORDER_ERROR uuid={uuid} err={type(e).__name__}: {e}")
            return None

    def _try_cancel_limit(self, uuid, side="BUY", ticker="", retries=3):
        """지정가 취소 재시도. 취소 확인 전에는 시장가 폴백 금지."""
        for attempt in range(1, int(retries) + 1):
            try:
                if self.upbit is None:
                    self.logger.warning(
                        f"CANCEL_ORDER_ERROR | side={side} ticker={ticker} "
                        f"uuid={uuid} try={attempt}/{retries} err=upbit_none"
                    )
                    time.sleep(0.2)
                    continue
                result = self.upbit.cancel_order(uuid)
                ok = result is not None
                self.logger.info(
                    f"LIMIT_ORDER_CANCEL_RESULT | side={side} ticker={ticker} "
                    f"uuid={uuid} ok={ok} try={attempt}/{retries}"
                )
                if ok:
                    return True
            except Exception as e:
                self.logger.warning(
                    f"CANCEL_ORDER_ERROR | side={side} ticker={ticker} "
                    f"uuid={uuid} try={attempt}/{retries} err={type(e).__name__}: {e}"
                )
            time.sleep(0.2)
        return False

    @staticmethod
    def _parse_risk_pct(value, default=0.004):
        try:
            pct = float(value)
        except Exception:
            pct = float(default)
        if pct >= 0.1:
            pct = pct / 100.0
        return max(0.0005, min(0.03, pct))

    @staticmethod
    def _normalize_ticker(ticker_or_symbol):
        value = str(ticker_or_symbol or "").upper().strip()
        if not value:
            return ""
        if value.startswith("KRW-"):
            return value
        if "-" in value:
            return f"KRW-{value.split('-')[-1]}"
        return f"KRW-{value}"

    @staticmethod
    def _symbol(ticker):
        value = str(ticker or "").upper()
        if "-" in value:
            return value.split("-")[-1]
        return value

    @staticmethod
    def _interval_to_minutes(interval):
        value = str(interval or "").lower()
        if value.startswith("minute"):
            try:
                return max(1, int(value.replace("minute", "")))
            except Exception:
                return 1
        if value == "day":
            return 1440
        if value == "week":
            return 10080
        return 1

    def _build_universe(self, strategy_cfg):
        raw_universe = strategy_cfg.get("universe")
        if not raw_universe:
            raw_universe = ["SOL", "DOGE", "ADA"]

        universe = []
        for value in raw_universe:
            ticker = self._normalize_ticker(value)
            if ticker and ticker not in universe:
                universe.append(ticker)
        return universe

    def _build_weight_caps(self, strategy_cfg):
        default_caps = {
            "SOL": 0.50,
            "DOGE": 0.40,
            "ADA": 0.35,
        }
        raw_caps = strategy_cfg.get("base_weight_caps", {}) or {}
        for key, value in raw_caps.items():
            symbol = self._symbol(key)
            weight = self._safe_float(value, default_caps.get(symbol, 0.0))
            default_caps[symbol] = max(0.0, min(1.0, weight))
        return default_caps

    def _build_risk_per_symbol(self, strategy_cfg, risk_cfg):
        default = {
            "SOL": 0.005,
            "DOGE": 0.004,
            "ADA": 0.003,
        }
        raw = strategy_cfg.get("risk_per_symbol_pct", {}) or risk_cfg.get("risk_per_symbol_pct", {}) or {}
        for key, value in raw.items():
            symbol = self._symbol(key)
            default[symbol] = self._parse_risk_pct(value, default.get(symbol, self.default_risk_per_trade_pct))
        return default

    def get_universe(self):
        return list(self.universe)

    def get_global_regime(self):
        return str(self.global_regime)

    def get_base_weight_cap(self, ticker):
        return float(self.base_weight_caps.get(self._symbol(ticker), 0.0))

    def _patch_pyupbit_remaining_req_parser(self):
        """Remaining-Req 헤더 파싱 실패로 인한 예외를 완화"""
        try:
            if getattr(request_api, "_patched_remaining_req_parser", False):
                return

            original_parse = request_api._parse

            def safe_parse(remaining_req):
                try:
                    return original_parse(remaining_req)
                except Exception:
                    pass

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
            if isinstance(orderbook, list) and orderbook:
                orderbook = orderbook[0]
            if not isinstance(orderbook, dict) or "orderbook_units" not in orderbook:
                return False, "호가 정보 없음", {"ticker": ticker}
            if not orderbook["orderbook_units"]:
                return False, "호가 정보 없음", {"ticker": ticker}

            top_unit = orderbook["orderbook_units"][0]
            ask_price = self._safe_float(top_unit.get("ask_price", 0))
            bid_price = self._safe_float(top_unit.get("bid_price", 0))
            ask_size = self._safe_float(top_unit.get("ask_size", 0))
            bid_size = self._safe_float(top_unit.get("bid_size", 0))

            details = {
                "ticker": ticker,
                "ask_price": ask_price,
                "bid_price": bid_price,
                "ask_size": ask_size,
                "bid_size": bid_size,
            }

            if ask_price <= 0 or bid_price <= 0:
                return False, "호가 가격 이상", details

            spread_pct = ((ask_price - bid_price) / bid_price) * 100
            details["spread_pct"] = float(spread_pct)
            if spread_pct > self.max_spread_pct:
                return False, f"스프레드 과다({spread_pct:.2f}%)", details

            top5 = orderbook["orderbook_units"][:5]
            bid_depth_krw_5 = 0.0
            ask_depth_krw_5 = 0.0
            bid_size_sum_5 = 0.0
            ask_size_sum_5 = 0.0
            for unit in top5:
                u_bid_price = self._safe_float(unit.get("bid_price", 0))
                u_ask_price = self._safe_float(unit.get("ask_price", 0))
                u_bid_size = self._safe_float(unit.get("bid_size", 0))
                u_ask_size = self._safe_float(unit.get("ask_size", 0))
                bid_depth_krw_5 += u_bid_price * u_bid_size
                ask_depth_krw_5 += u_ask_price * u_ask_size
                bid_size_sum_5 += u_bid_size
                ask_size_sum_5 += u_ask_size

            details["bid_depth_krw"] = float(bid_price * bid_size)
            details["ask_depth_krw"] = float(ask_price * ask_size)
            details["bid_depth_krw_5"] = float(bid_depth_krw_5)
            details["ask_depth_krw_5"] = float(ask_depth_krw_5)
            details["bid_size_sum_5"] = float(bid_size_sum_5)
            details["ask_size_sum_5"] = float(ask_size_sum_5)

            min_depth_krw_5 = min(bid_depth_krw_5, ask_depth_krw_5)
            if min_depth_krw_5 < self.min_orderbook_depth:
                self._throttled_info(
                    f"buy_block_low_liquidity:{ticker}",
                    (
                        f"BUY_BLOCKED: LOW_LIQUIDITY | ticker={ticker} "
                        f"depth_min_krw={min_depth_krw_5:,.0f} "
                        f"threshold={self.min_orderbook_depth:,.0f}"
                    ),
                    bucket_seconds=30,
                )
                return False, f"LOW_LIQUIDITY({min_depth_krw_5:,.0f}원)", details

            return True, "안전", details
        except Exception as e:
            return False, f"호가 체크 오류: {e}", {"ticker": ticker, "error": f"{type(e).__name__}: {e}"}

    def connect(self, access_key, secret_key):
        """업비트 API 연결"""
        try:
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

    def _calc_rsi(self, close):
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _calc_true_range(self, df):
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

    def _calc_atr(self, df):
        true_range = self._calc_true_range(df)
        return true_range.rolling(self.atr_period).mean()

    def _get_cached_ohlcv(self, ticker, interval="minute1", count=200, ttl_seconds=2):
        """OHLCV 조회 with 단기 캐시 (요청 수 제한 완화)."""
        now = time.time()
        key = (ticker, interval, int(count))

        if ttl_seconds and key in self._ohlcv_cache:
            ts, cached_df = self._ohlcv_cache[key]
            if (now - ts) < ttl_seconds and cached_df is not None:
                return cached_df.copy()

        count_int = max(1, int(count))
        if count_int <= 200:
            df = pyupbit.get_ohlcv(ticker, interval=interval, count=count_int)
        else:
            frames = []
            remain = count_int
            to = None
            while remain > 0:
                batch = min(200, remain)
                if to is None:
                    part = pyupbit.get_ohlcv(ticker, interval=interval, count=batch)
                else:
                    part = pyupbit.get_ohlcv(ticker, interval=interval, count=batch, to=to)
                if part is None or len(part) == 0:
                    break
                frames.append(part)
                remain -= len(part)
                first_ts = pd.Timestamp(part.index[0])
                # pyupbit의 `to`는 UTC 문자열 해석이 가장 안정적이다.
                to_utc = first_ts - pd.Timedelta(hours=9, seconds=1)
                to = to_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                if len(part) < batch:
                    break
                time.sleep(0.05)

            if frames:
                df = pd.concat(frames).sort_index()
                df = df[~df.index.duplicated(keep="first")]
                df = df.tail(count_int)
            else:
                df = None

        if df is not None:
            self._ohlcv_cache[key] = (now, df.copy())
        return df

    def _get_resampled_ohlcv(self, ticker, minutes=20, count=220, ttl_seconds=4):
        """5분봉을 기반으로 N분봉으로 리샘플링."""
        base_minutes = 5
        factor = max(1, int(minutes // base_minutes))
        base_count = max(200, int(count * factor + 40))

        df = self._get_cached_ohlcv(
            ticker=ticker,
            interval=f"minute{base_minutes}",
            count=base_count,
            ttl_seconds=ttl_seconds,
        )
        if df is None or len(df) < max(80, factor * 20):
            return None

        work = df.copy()
        if not isinstance(work.index, pd.DatetimeIndex):
            work.index = pd.to_datetime(work.index)

        rule = f"{int(minutes)}min"
        resampled = (
            work.resample(rule, label="right", closed="right")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                    "value": "sum",
                }
            )
            .dropna()
        )

        if len(resampled) < 210:
            return None

        if int(minutes) == 20 and len(resampled) >= 2:
            last_closed = str(resampled.index[-2])
            prev = self._last_resample_closed_ts.get(ticker)
            if prev != last_closed:
                self._last_resample_closed_ts[ticker] = last_closed
                self.logger.info(f"Resampled to 20min for {ticker}, last closed candle: {last_closed}")

        return resampled.tail(max(count, 210))

    def _is_entry_time_blocked(self):
        now_hour = datetime.now().hour
        start = int(self.entry_block_start_hour)
        end = int(self.entry_block_end_hour)
        if start == end:
            return False
        if start < end:
            return start <= now_hour < end
        return now_hour >= start or now_hour < end

    def _risk_pct_for_symbol(self, ticker):
        symbol = self._symbol(ticker)
        return float(self.risk_per_symbol_pct.get(symbol, self.default_risk_per_trade_pct))

    def _check_btc_trend_filter(self):
        if not self.btc_filter_enabled:
            return True, {"enabled": False}

        df = self._get_resampled_ohlcv(
            self.btc_filter_ticker,
            minutes=self.signal_candle_minutes,
            count=max(220, self.btc_filter_ema_period + 40),
            ttl_seconds=10,
        )
        if df is None or len(df) < self.btc_filter_ema_period + 2:
            self._throttled_info("btc_filter_short", "BUY_BLOCKED: BTC_FILTER (btc_data_short)", bucket_seconds=60)
            return False, {"enabled": True, "reason": "btc_data_short"}

        work = df.copy()
        work["ema_btc"] = work["close"].ewm(span=self.btc_filter_ema_period, adjust=False).mean()
        row = work.iloc[-2]
        close = self._safe_float(row.get("close", 0), 0)
        ema = self._safe_float(row.get("ema_btc", 0), 0)
        passed = close > ema > 0
        meta = {
            "enabled": True,
            "ticker": self.btc_filter_ticker,
            "close": float(close),
            "ema": float(ema),
            "passed": bool(passed),
            "candle_ts": str(getattr(row, "name", "") or ""),
        }
        sig = (meta["candle_ts"], bool(passed))
        if sig != self._last_btc_filter_signature:
            self._last_btc_filter_signature = sig
            if passed:
                self.logger.info(
                    f"BTC_FILTER PASS | close={close:,.0f} ema{self.btc_filter_ema_period}={ema:,.0f}"
                )
            else:
                self.logger.info(
                    f"BUY_BLOCKED: BTC_FILTER | close={close:,.0f} ema{self.btc_filter_ema_period}={ema:,.0f}"
                )
        return passed, meta

    def _persist_position_meta(self, ticker, position, buy_meta):
        if not isinstance(position, dict):
            return
        position["buy_meta"] = buy_meta if isinstance(buy_meta, dict) else {}
        try:
            self.stats.save_positions()
        except Exception:
            pass

    def _classify_structure(self, df):
        """HH/HL vs LH/LL 단순 구조 분류"""
        if df is None or len(df) < 30:
            return "UNKNOWN"

        highs = df["high"].iloc[-30:-2]
        lows = df["low"].iloc[-30:-2]
        if len(highs) < 20 or len(lows) < 20:
            return "UNKNOWN"

        pivot = int(len(highs) * 0.5)
        older_high = self._safe_float(highs.iloc[:pivot].max(), 0)
        recent_high = self._safe_float(highs.iloc[pivot:].max(), 0)
        older_low = self._safe_float(lows.iloc[:pivot].min(), 0)
        recent_low = self._safe_float(lows.iloc[pivot:].min(), 0)

        if older_high <= 0 or older_low <= 0:
            return "UNKNOWN"

        hh = recent_high > older_high * 1.001
        hl = recent_low > older_low * 0.999
        lh = recent_high < older_high * 0.999
        ll = recent_low < older_low * 1.001

        if hh and hl:
            return "BULL"
        if lh and ll:
            return "BEAR"
        return "RANGE"

    def _estimate_equity_krw(self):
        cash = self.get_balance("KRW")
        total = float(cash or 0)

        for ticker, pos in list(self.stats.positions.items()):
            try:
                price = self.get_current_price(ticker)
                if not price:
                    price = self._safe_float(pos.get("buy_price", 0), 0)
                amount = self._safe_float(pos.get("amount", 0), 0)
                total += float(price) * amount
            except Exception:
                continue
        return float(total)

    def _estimate_total_invested_cost(self):
        total = 0.0
        for _, pos in list(self.stats.positions.items()):
            try:
                total += self._safe_float(pos.get("buy_price", 0), 0) * self._safe_float(pos.get("amount", 0), 0)
            except Exception:
                continue
        return float(total)

    def _estimate_position_exposure_krw(self, ticker):
        pos = self.stats.positions.get(ticker)
        if not pos:
            return 0.0
        price = self.get_current_price(ticker)
        if not price:
            price = self._safe_float(pos.get("buy_price", 0), 0)
        return self._safe_float(price, 0) * self._safe_float(pos.get("amount", 0), 0)

    def _size_by_risk(self, ticker, entry_price, stop_price):
        stop_distance = abs(float(entry_price) - float(stop_price))
        if stop_distance <= 0:
            return {
                "equity_krw": 0.0,
                "risk_krw": 0.0,
                "risk_pct": 0.0,
                "qty_by_risk": 0.0,
                "risk_invest_krw": 0.0,
                "weight_cap_krw": 0.0,
                "weight_remaining_krw": 0.0,
                "total_cap_remaining_krw": 0.0,
                "recommended_invest_krw": 0.0,
            }

        equity = self._estimate_equity_krw()
        risk_pct = self._risk_pct_for_symbol(ticker)
        risk_krw = equity * risk_pct
        qty_by_risk = risk_krw / stop_distance
        risk_invest_krw = qty_by_risk * entry_price

        weight_cap_krw = equity * self.get_base_weight_cap(ticker)
        if weight_cap_krw <= 0:
            weight_cap_krw = self.max_total_investment
        current_exposure = self._estimate_position_exposure_krw(ticker)
        weight_remaining_krw = max(0.0, weight_cap_krw - current_exposure)

        total_invested = self._estimate_total_invested_cost()
        total_cap_remaining = max(0.0, self.max_total_investment - total_invested)
        recommended = min(risk_invest_krw, weight_remaining_krw, total_cap_remaining)

        return {
            "equity_krw": float(equity),
            "risk_krw": float(risk_krw),
            "risk_pct": float(risk_pct * 100.0),
            "qty_by_risk": float(max(0.0, qty_by_risk)),
            "risk_invest_krw": float(max(0.0, risk_invest_krw)),
            "weight_cap_krw": float(weight_cap_krw),
            "weight_remaining_krw": float(weight_remaining_krw),
            "total_cap_remaining_krw": float(total_cap_remaining),
            "recommended_invest_krw": float(max(0.0, recommended)),
        }

    def detect_global_regime(self):
        """20분봉 EMA50/EMA200 기반 레짐 후보 계산."""
        df = self._get_resampled_ohlcv(
            self.regime_reference_ticker,
            minutes=self.signal_candle_minutes,
            count=260,
            ttl_seconds=12,
        )
        if df is None or len(df) < 210:
            return "RANGE", {"reason": "global_data_short"}

        df = df.copy()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
        cur = df.iloc[-2]

        close = self._safe_float(cur.get("close", 0), 0)
        ema50 = self._safe_float(cur.get("ema50", 0), 0)
        ema200 = self._safe_float(cur.get("ema200", 0), 0)

        if close > ema50 > ema200:
            candidate = "BULL"
        elif close < ema50 < ema200:
            candidate = "BEAR"
        else:
            candidate = "RANGE"

        meta = {
            "reference_ticker": self.regime_reference_ticker,
            "close": float(close),
            "ema50": float(ema50),
            "ema200": float(ema200),
            "candidate": candidate,
            "candle_ts": str(getattr(cur, "name", "") or ""),
        }
        return candidate, meta

    def update_global_regime(self, force=False):
        """3연속 확인으로 레짐 전환 확정."""
        now = datetime.now()
        if not force and self._last_regime_check:
            elapsed = (now - self._last_regime_check).total_seconds()
            if elapsed < max(60, self.regime_check_minutes * 60):
                return self.global_regime, {"skipped": True}

        self._last_regime_check = now
        candidate, detect_meta = self.detect_global_regime()
        previous_regime = self.global_regime

        if candidate == self.global_regime:
            self._regime_candidate = None
            self._regime_candidate_count = 0
            applied = False
        else:
            if candidate == self._regime_candidate:
                self._regime_candidate_count += 1
            else:
                self._regime_candidate = candidate
                self._regime_candidate_count = 1

            can_switch = True
            if self.regime_min_hold_minutes > 0 and self._regime_changed_at:
                held_minutes = (now - self._regime_changed_at).total_seconds() / 60.0
                can_switch = held_minutes >= self.regime_min_hold_minutes

            if self._regime_candidate_count >= self.regime_confirm_count and can_switch:
                self.global_regime = candidate
                self._regime_changed_at = now
                self._regime_candidate = None
                self._regime_candidate_count = 0
                applied = True
            else:
                applied = False

        payload = {
            "current": self.global_regime,
            "previous": previous_regime,
            "candidate": candidate,
            "candidate_count": int(self._regime_candidate_count),
            "confirm_count": int(self.regime_confirm_count),
            "min_hold_minutes": int(self.regime_min_hold_minutes),
            "applied": bool(applied),
            "force": bool(force),
            "detect": detect_meta,
        }
        self.logger.log_decision("REGIME_UPDATE", payload)
        self._throttled_info(
            "regime_candidate",
            (
                f"Regime candidate: {candidate}, "
                f"confirm_count: {int(self._regime_candidate_count)}/{int(self.regime_confirm_count)}, "
                f"current: {self.global_regime}"
            ),
            bucket_seconds=30,
        )
        if applied:
            self.logger.info(f"📈 글로벌 레짐 전환: {previous_regime} -> {self.global_regime}")
        return self.global_regime, payload

    def analyze_symbol(self, ticker):
        """20분봉 기반 전략 상태 계산."""
        lookback = max(self.analysis_lookback, self.ada_range_lookback + 20, self.sol_breakout_lookback + 20, 220)
        df = self._get_resampled_ohlcv(
            ticker=ticker,
            minutes=self.signal_candle_minutes,
            count=lookback,
            ttl_seconds=4,
        )
        if df is None or len(df) < 210:
            return None

        df = df.copy()
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
        df["rsi"] = self._calc_rsi(df["close"])
        df["tr"] = self._calc_true_range(df)
        df["atr"] = self._calc_atr(df)
        df["volume_ma20"] = df["volume"].rolling(20).mean()

        cur = df.iloc[-2]
        prev = df.iloc[-3]
        close = self._safe_float(cur.get("close", 0), 0)
        prev_close = self._safe_float(prev.get("close", close), close)
        high = self._safe_float(cur.get("high", close), close)
        low = self._safe_float(cur.get("low", close), close)
        tr = self._safe_float(cur.get("tr", 0), 0)
        atr = self._safe_float(cur.get("atr", 0), 0)
        rsi = self._safe_float(cur.get("rsi", 50), 50)
        ema20 = self._safe_float(cur.get("ema20", close), close)
        ema50 = self._safe_float(cur.get("ema50", close), close)
        ema200 = self._safe_float(cur.get("ema200", close), close)
        volume = self._safe_float(cur.get("volume", 0), 0)
        volume_ma = self._safe_float(cur.get("volume_ma20", 0), 0)
        volume_ratio = (volume / volume_ma) if volume_ma > 0 else 0

        breakout_window = df["high"].iloc[-(self.sol_breakout_lookback + 2):-2]
        breakout_level = self._safe_float(breakout_window.max(), close) if len(breakout_window) > 0 else close
        breakout_above = bool(close > breakout_level and high >= breakout_level)
        retest_band = max(atr * self.sol_retest_atr_tolerance, close * 0.0015)
        retest_ok_sol = (
            breakout_above
            and low <= (breakout_level + retest_band)
            and close >= breakout_level
            and abs(close - breakout_level) <= max(retest_band * 1.4, close * 0.01)
        )

        swing_window = df.iloc[-(self.ada_range_lookback + 2):-2]
        swing_high = self._safe_float(swing_window["high"].max(), close) if len(swing_window) > 0 else close
        swing_low = self._safe_float(swing_window["low"].min(), close) if len(swing_window) > 0 else close
        range_width = max(0.0, swing_high - swing_low)
        range_position = ((close - swing_low) / range_width) if range_width > 0 else 0.5
        middle_zone = 0.40 <= range_position <= 0.60
        range_bounce = close >= prev_close
        ada_in_lower_zone = bool(range_position <= self.ada_entry_lower_pct)
        ada_target_price = swing_low + (range_width * self.ada_take_profit_upper_pct)

        tr_atr_ratio = (tr / atr) if atr > 0 else 0.0
        atr_pct = (atr / close) * 100 if close > 0 else 0.0
        range_width_pct = (range_width / close) * 100 if close > 0 else 0.0

        if close > ema50 > ema200:
            structure = "BULL"
        elif close < ema50 < ema200:
            structure = "BEAR"
        else:
            structure = "RANGE"

        pullback_to_ema20 = abs(close - ema20) <= max(atr * self.doge_pullback_atr_tolerance, close * 0.0025)
        volatility_ok = tr_atr_ratio <= self.volatility_tr_atr_max
        trend_bias_pct = ((ema50 / ema200) - 1.0) * 100.0 if ema200 > 0 else 0.0

        quality_score = 0.0
        quality_score += 20.0 if volatility_ok else -20.0
        quality_score += min(18.0, max(0.0, volume_ratio * 9.0))
        quality_score += 10.0 if breakout_above else 0.0
        quality_score += 8.0 if retest_ok_sol else 0.0
        quality_score += 8.0 if pullback_to_ema20 else 0.0
        quality_score += 10.0 if ada_in_lower_zone else 0.0
        quality_score += 8.0 if range_bounce else 0.0

        return {
            "ticker": ticker,
            "candle_ts": str(getattr(cur, "name", "") or ""),
            "close": float(close),
            "prev_close": float(prev_close),
            "high": float(high),
            "low": float(low),
            "ema20": float(ema20),
            "ema50": float(ema50),
            "ema200": float(ema200),
            "rsi": float(rsi),
            "atr": float(atr),
            "atr_pct": float(atr_pct),
            "tr": float(tr),
            "tr_atr_ratio": float(tr_atr_ratio),
            "volume_ratio": float(volume_ratio),
            "breakout_level": float(breakout_level),
            "swing_high": float(swing_high),
            "swing_low": float(swing_low),
            "range_width_pct": float(range_width_pct),
            "range_position": float(max(0.0, min(1.0, range_position))),
            "middle_zone": bool(middle_zone),
            "range_clarity": bool(structure == "RANGE"),
            "retest_ok_bull": bool(retest_ok_sol),
            "retest_ok_sol": bool(retest_ok_sol),
            "breakout_above_48": bool(breakout_above),
            "near_lower_extreme": bool(ada_in_lower_zone),
            "range_bounce": bool(range_bounce),
            "volatility_ok": bool(volatility_ok),
            "structure": structure,
            "symbol_regime": structure,
            "trend_bias_pct": float(trend_bias_pct),
            "pullback_to_ema20": bool(pullback_to_ema20),
            "ada_in_lower_zone": bool(ada_in_lower_zone),
            "ada_target_price": float(ada_target_price),
            "quality_score": float(quality_score),
        }

    def select_strategy(self, symbol_state, global_regime=None):
        regime = str(global_regime or self.global_regime)
        if not symbol_state:
            return None

        symbol = self._symbol(symbol_state.get("ticker"))
        if symbol == "SOL":
            return "SOL_TREND" if regime == "BULL" else None
        if symbol == "DOGE":
            return "DOGE_MOMENTUM"
        if symbol == "ADA":
            return "ADA_RANGE" if regime == "RANGE" else None
        return None

    def check_buy_signal(self, ticker):
        """스펙 기반 매수 시그널 판단 (안전 실행 순서 강제)."""
        try:
            # 1) 글로벌 레짐
            regime, _ = self.update_global_regime(force=False)
            if regime == "BEAR":
                self._throttled_info("buy_block_global_bear", "BUY_BLOCKED: GLOBAL_BEAR", bucket_seconds=60)
                return False, [], None, 0, {
                    "ticker": ticker,
                    "global_regime": regime,
                    "blocked_by": ["GLOBAL_BEAR"],
                }

            # 2) BTC 필터
            btc_filter_passed, btc_filter_meta = self._check_btc_trend_filter()
            if not btc_filter_passed:
                self._throttled_info("buy_block_btc", "BUY_BLOCKED: BTC_FILTER", bucket_seconds=60)
                return False, [], None, 0, {
                    "ticker": ticker,
                    "global_regime": regime,
                    "btc_filter": btc_filter_meta,
                    "blocked_by": ["BTC_FILTER"],
                }

            # 3) 시간 필터
            if self._is_entry_time_blocked():
                self._throttled_info("buy_block_time", "BUY_BLOCKED: ENTRY_TIME_BLOCKED", bucket_seconds=60)
                return False, [], None, 0, {
                    "ticker": ticker,
                    "global_regime": regime,
                    "btc_filter": btc_filter_meta,
                    "blocked_by": ["ENTRY_TIME_BLOCKED"],
                }

            # 전략 데이터 준비
            state = self.analyze_symbol(ticker)
            if not state:
                return False, ["데이터부족"], None, 0, {"blocked_by": ["데이터부족"]}
            entry_price = state["close"]

            # 4) 변동성 필터
            tr_atr_ratio = float(state.get("tr_atr_ratio", 0) or 0)
            if tr_atr_ratio > self.volatility_tr_atr_max:
                self._throttled_info(
                    "buy_block_volatility",
                    f"BUY_BLOCKED: VOLATILITY_FILTER ({ticker}, tr/atr={tr_atr_ratio:.2f})",
                    bucket_seconds=60,
                )
                return False, [], entry_price, 0, {
                    "ticker": ticker,
                    "global_regime": regime,
                    "btc_filter": btc_filter_meta,
                    "tr_atr_ratio": float(tr_atr_ratio),
                    "blocked_by": ["VOLATILITY_FILTER"],
                }

            # 5) 최대 포지션 수
            if len(self.stats.positions) >= self.max_positions:
                self._throttled_info("buy_block_max_positions", "BUY_BLOCKED: MAX_POSITIONS", bucket_seconds=30)
                return False, [], entry_price, 0, {
                    "ticker": ticker,
                    "global_regime": regime,
                    "btc_filter": btc_filter_meta,
                    "max_positions": int(self.max_positions),
                    "current_positions": int(len(self.stats.positions)),
                    "blocked_by": ["MAX_POSITIONS"],
                }

            strategy = self.select_strategy(state, regime)
            signals = []
            blocked_by = []
            score = 0

            def block(reason):
                if reason not in blocked_by:
                    blocked_by.append(reason)

            if strategy is None:
                block("레짐전략불일치")
            else:
                signals.append(f"레짐:{regime}")
                signals.append(f"전략:{strategy}")
                score += 2

            stop_price = None
            take_profit_price = None
            time_stop_candles = self.time_stop_candles
            target_r = None

            if strategy == "SOL_TREND":
                if not state.get("breakout_above_48"):
                    block("SOL48고점돌파미충족")
                else:
                    score += 2
                    signals.append("48봉돌파")

                if not state.get("retest_ok_sol"):
                    block("SOL리테스트미충족")
                else:
                    score += 2
                    signals.append("리테스트")

                stop_price = entry_price - (self.sol_stop_atr * state.get("atr", 0))
                target_r = self.sol_partial_tp_r

            elif strategy == "DOGE_MOMENTUM":
                if state.get("volume_ratio", 0) < self.doge_volume_spike_min:
                    block("DOGE거래량스파이크미충족")
                else:
                    score += 2
                    signals.append("거래량스파이크")

                if state.get("rsi", 0) <= self.doge_rsi_min:
                    block("DOGERSI미충족")
                else:
                    score += 1
                    signals.append(f"RSI>{self.doge_rsi_min:.0f}")

                if not state.get("pullback_to_ema20"):
                    block("EMA20풀백미충족")
                else:
                    score += 2
                    signals.append("EMA20풀백")

                stop_price = entry_price * (1.0 - self.doge_stop_pct)
                time_stop_candles = self.doge_time_stop_candles
                target_r = self.doge_target_r

            elif strategy == "ADA_RANGE":
                if state.get("rsi", 100) > self.ada_rsi_max:
                    block("ADARSI과매도미충족")
                else:
                    score += 1
                    signals.append(f"RSI<={self.ada_rsi_max:.0f}")

                if not state.get("ada_in_lower_zone"):
                    block("ADA하단15%미충족")
                else:
                    score += 2
                    signals.append("하단15%")

                stop_price = entry_price * (1.0 - self.ada_stop_pct)
                take_profit_price = state.get("ada_target_price")

            if strategy and (not stop_price or stop_price <= 0 or stop_price >= entry_price):
                block("손절가산출실패")
                stop_price = entry_price * (1.0 + min(self.stop_loss, -0.004))

            # 6) 포지션 사이즈 계산
            if strategy:
                sizing = self._size_by_risk(ticker, entry_price, stop_price or entry_price)
            else:
                sizing = {
                    "equity_krw": 0.0,
                    "risk_krw": 0.0,
                    "risk_pct": 0.0,
                    "qty_by_risk": 0.0,
                    "risk_invest_krw": 0.0,
                    "weight_cap_krw": 0.0,
                    "weight_remaining_krw": 0.0,
                    "total_cap_remaining_krw": 0.0,
                    "recommended_invest_krw": 0.0,
                }

            min_trade = float(self.config.get("trading", {}).get("min_trade_amount", 5500))
            if strategy and sizing["recommended_invest_krw"] < min_trade:
                block("리스크사이징최소금액미달")

            if score < 3:
                block("점수부족")

            risk_unit = max(0.0, entry_price - stop_price) if stop_price else 0.0
            meta = {
                "ticker": ticker,
                "strategy_mode": self.strategy_mode,
                "global_regime": regime,
                "symbol_regime": state.get("symbol_regime"),
                "strategy": strategy,
                "candle_ts": state["candle_ts"],
                "close": float(entry_price),
                "rsi": float(state["rsi"]),
                "atr": float(state["atr"]),
                "atr_pct": float(state["atr_pct"]),
                "tr_atr_ratio": float(tr_atr_ratio),
                "volume_ratio": float(state["volume_ratio"]),
                "range_position": float(state["range_position"]),
                "middle_zone": bool(state["middle_zone"]),
                "breakout_level": float(state["breakout_level"]),
                "swing_low": float(state["swing_low"]),
                "swing_high": float(state["swing_high"]),
                "stop_price": float(stop_price) if stop_price else None,
                "take_profit_price": float(take_profit_price) if take_profit_price else None,
                "time_stop_candles": int(time_stop_candles),
                "risk_unit": float(risk_unit),
                "target_r": float(target_r) if target_r is not None else None,
                "risk_per_trade_pct": float(sizing.get("risk_pct", 0)),
                "btc_filter": btc_filter_meta,
                "signals": list(signals),
                "score": int(score),
                "quality_score": float(state.get("quality_score", 0.0)),
                "blocked_by": list(blocked_by),
            }
            if strategy == "SOL_TREND":
                meta.update(
                    {
                        "tp1_r": float(self.sol_partial_tp_r),
                        "trail_activate_r": float(self.sol_trailing_activate_r),
                        "sol_trailing_stop_pct": float(self.sol_trailing_stop_pct),
                    }
                )
            elif strategy == "DOGE_MOMENTUM":
                meta.update(
                    {
                        "target_r": float(self.doge_target_r),
                        "time_stop_candles": int(self.doge_time_stop_candles),
                    }
                )
            elif strategy == "ADA_RANGE":
                meta.update(
                    {
                        "take_profit_price": float(take_profit_price) if take_profit_price else None,
                    }
                )
            meta.update(sizing)

            if blocked_by:
                return False, signals, entry_price, score, meta

            return True, signals, entry_price, score, meta

        except Exception as e:
            self.logger.log_error(f"{ticker} 매수 신호 확인 오류", e)
            return False, [], None, 0, {"blocked_by": ["예외"], "error": f"{type(e).__name__}: {e}"}

    def check_sell_signal(self, ticker, position):
        """전략별 청산 시그널 판단."""
        try:
            state = self.analyze_symbol(ticker)
            current_price = self.get_current_price(ticker)
            if current_price is None and state:
                current_price = state.get("close")
            if current_price is None:
                return False, "HOLD", 1.0, {"blocked_by": ["가격조회실패"]}

            buy_price = self._safe_float(position.get("buy_price", 0), 0)
            if buy_price <= 0:
                return False, "HOLD", 1.0, {"blocked_by": ["매수가없음"]}

            highest_price = self._safe_float(position.get("highest_price", buy_price), buy_price)
            if current_price > highest_price:
                highest_price = current_price
                self.stats.update_position_highest(ticker, highest_price)

            hold_minutes = 0.0
            try:
                hold_minutes = (datetime.now() - position["timestamp"]).total_seconds() / 60.0
            except Exception:
                hold_minutes = 0.0

            buy_meta = position.get("buy_meta", {}) if isinstance(position.get("buy_meta"), dict) else {}
            strategy = buy_meta.get("strategy")
            stop_price = self._safe_float(buy_meta.get("stop_price", 0), 0)
            if stop_price <= 0:
                stop_price = buy_price * (1 + self.stop_loss)

            profit_rate = (current_price - buy_price) / buy_price
            risk_unit = max(1e-8, buy_price - stop_price)
            progress_r = (current_price - buy_price) / risk_unit
            hold_candles = hold_minutes / max(1, self.signal_candle_minutes)

            meta = {
                "ticker": ticker,
                "current_price": float(current_price),
                "buy_price": float(buy_price),
                "highest_price": float(highest_price),
                "profit_rate": float(profit_rate),
                "hold_minutes": float(hold_minutes),
                "hold_candles": float(hold_candles),
                "strategy": strategy,
                "global_regime": self.global_regime,
                "stop_price": float(stop_price),
                "risk_unit": float(risk_unit),
                "progress_r": float(progress_r),
            }

            if current_price <= stop_price:
                reason = f"구조손절({profit_rate*100:.2f}%)"
                meta["reason"] = reason
                meta["r_multiple"] = float(progress_r)
                return True, reason, 1.0, meta

            if strategy == "SOL_TREND":
                tp1_done = bool(buy_meta.get("sol_tp1_done", False))
                tp1_r = self._safe_float(buy_meta.get("tp1_r", self.sol_partial_tp_r), self.sol_partial_tp_r)
                trail_activate_r = self._safe_float(
                    buy_meta.get("trail_activate_r", self.sol_trailing_activate_r),
                    self.sol_trailing_activate_r,
                )
                trailing_pct = self._safe_float(
                    buy_meta.get("sol_trailing_stop_pct", self.sol_trailing_stop_pct),
                    self.sol_trailing_stop_pct,
                )

                if (not tp1_done) and progress_r >= tp1_r:
                    buy_meta["sol_tp1_done"] = True
                    buy_meta["tp1_executed_at"] = datetime.now().isoformat()
                    self._persist_position_meta(ticker, position, buy_meta)
                    reason = f"SOL 1차익절({progress_r:.2f}R)"
                    meta["reason"] = reason
                    meta["r_multiple"] = float(progress_r)
                    return True, reason, 0.30, meta

                trailing_active = bool(buy_meta.get("sol_trailing_active", False))
                if (not trailing_active) and progress_r >= trail_activate_r:
                    trailing_active = True
                    buy_meta["sol_trailing_active"] = True
                    buy_meta["sol_trailing_stop_price"] = float(
                        max(stop_price, highest_price * (1.0 - trailing_pct))
                    )
                    buy_meta["sol_trailing_started_at"] = datetime.now().isoformat()
                    self._persist_position_meta(ticker, position, buy_meta)

                if trailing_active:
                    prev_trailing = self._safe_float(
                        buy_meta.get("sol_trailing_stop_price", stop_price),
                        stop_price,
                    )
                    new_trailing = max(prev_trailing, highest_price * (1.0 - trailing_pct))
                    if new_trailing > prev_trailing * 1.000001:
                        buy_meta["sol_trailing_stop_price"] = float(new_trailing)
                        self._persist_position_meta(ticker, position, buy_meta)

                    meta["trailing_stop_price"] = float(new_trailing)
                    if current_price <= new_trailing:
                        reason = f"SOL 트레일링청산({progress_r:.2f}R)"
                        meta["reason"] = reason
                        meta["r_multiple"] = float(progress_r)
                        return True, reason, 1.0, meta

            elif strategy == "DOGE_MOMENTUM":
                target_r = self._safe_float(buy_meta.get("target_r", self.doge_target_r), self.doge_target_r)
                time_stop_candles = int(
                    self._safe_float(
                        buy_meta.get("time_stop_candles", self.doge_time_stop_candles),
                        self.doge_time_stop_candles,
                    )
                )
                meta["target_r"] = float(target_r)
                meta["time_stop_candles"] = int(time_stop_candles)

                if progress_r >= target_r:
                    reason = f"DOGE 목표도달({progress_r:.2f}R)"
                    meta["reason"] = reason
                    meta["r_multiple"] = float(progress_r)
                    return True, reason, 1.0, meta

                if hold_candles >= time_stop_candles and progress_r < target_r:
                    reason = f"DOGE 시간청산({hold_candles:.1f}캔들,{progress_r:.2f}R)"
                    meta["reason"] = reason
                    meta["r_multiple"] = float(progress_r)
                    return True, reason, 1.0, meta

            elif strategy == "ADA_RANGE":
                target_price = self._safe_float(buy_meta.get("take_profit_price", 0), 0)
                if target_price > 0:
                    meta["take_profit_price"] = float(target_price)
                    if current_price >= target_price:
                        reason = f"ADA 목표청산({progress_r:.2f}R)"
                        meta["reason"] = reason
                        meta["r_multiple"] = float(progress_r)
                        return True, reason, 1.0, meta

            if state:
                meta["symbol_regime"] = state.get("symbol_regime")
                meta["range_position"] = float(state.get("range_position", 0.5))
                meta["rsi"] = float(state.get("rsi", 50))
                meta["tr_atr_ratio"] = float(state.get("tr_atr_ratio", 0))

            if self.max_hold_minutes > 0 and hold_minutes >= self.max_hold_minutes:
                reason = f"최대보유청산({hold_minutes:.0f}m,{profit_rate*100:.2f}%)"
                meta["reason"] = reason
                meta["r_multiple"] = float(progress_r)
                return True, reason, 1.0, meta

            return False, "HOLD", 1.0, meta

        except Exception as e:
            self.logger.log_error(f"{ticker} 매도 신호 확인 오류", e)
            return False, "ERROR", 1.0, {"blocked_by": ["예외"], "error": f"{type(e).__name__}: {e}"}
    
    def execute_buy(self, ticker, invest_amount):
        """매수 실행 - 지정가 우선, 부분체결 안전 처리"""
        
        try:
            if ticker not in self.stats.positions and len(self.stats.positions) >= self.max_positions:
                self._throttled_info("buy_block_exec_max_positions", "BUY_BLOCKED: MAX_POSITIONS", bucket_seconds=30)
                return None

            current_price = pyupbit.get_current_price(ticker)
            if current_price is None:
                return None
            
            # 주문 방식 결정
            if self.order_type == 'limit_with_fallback':
                # 1단계: 지정가 주문 시도
                orderbook = pyupbit.get_orderbook(ticker)
                if isinstance(orderbook, list) and len(orderbook) > 0:
                    orderbook = orderbook[0]

                if isinstance(orderbook, dict) and "orderbook_units" in orderbook and orderbook["orderbook_units"]:
                    bid_price = orderbook['orderbook_units'][0]['bid_price']
                    if bid_price <= 0:
                        return None
                    buy_amount = round(invest_amount / bid_price, 8)
                    if buy_amount <= 0:
                        return None
                    
                    self.logger.info(
                        f"LIMIT_ORDER_ATTEMPT | side=BUY ticker={ticker} price={bid_price:,.0f} qty={buy_amount:.8f}"
                    )
                    
                    # 지정가 주문
                    result = self.upbit.buy_limit_order(ticker, bid_price, buy_amount)
                    
                    if result and 'uuid' in result:
                        order_uuid = result['uuid']
                        self.logger.info(
                            f"LIMIT_ORDER_PLACED | side=BUY ticker={ticker} "
                            f"uuid={order_uuid} price={bid_price:,.0f} qty={buy_amount:.8f}"
                        )
                        self.logger.info("Limit order placed, waiting fill...")

                        try:
                            poll_interval = float(self.config.get("trading", {}).get("limit_poll_interval_seconds", 0.3))
                        except Exception:
                            poll_interval = 0.3
                        poll_interval = max(0.1, min(2.0, poll_interval))
                        wait_seconds = max(0.0, float(self.limit_wait_seconds))
                        deadline = time.time() + wait_seconds
                        first_poll = True
                        min_trade_amount = float(self.config.get("trading", {}).get("min_trade_amount", 5500))

                        # 지정가 상태를 짧은 간격으로 확인(단, wait_seconds=0이어도 최소 1회 조회)
                        while first_poll or time.time() < deadline:
                            first_poll = False
                            order_info = self._safe_get_order(order_uuid)
                            if order_info is None:
                                self.logger.warning(
                                    f"LIMIT_ORDER_STATUS_UNKNOWN | side=BUY ticker={ticker} "
                                    f"uuid={order_uuid} action=cancel_before_fallback"
                                )
                                cancel_ok = self._try_cancel_limit(order_uuid, side="BUY", ticker=ticker, retries=3)
                                self.logger.info(
                                    f"LIMIT_ORDER_TIMEOUT_CANCEL_RESULT | side=BUY ticker={ticker} "
                                    f"uuid={order_uuid} ok={cancel_ok} reason=status_unknown"
                                )
                                if cancel_ok:
                                    self.logger.warning(
                                        f"ABORT_FALLBACK_UNKNOWN_STATE | side=BUY ticker={ticker} "
                                        f"uuid={order_uuid} cancel_ok=True"
                                    )
                                else:
                                    self.logger.error(
                                        f"CANCEL_FAILED_UNKNOWN_STATE | side=BUY ticker={ticker} "
                                        f"uuid={order_uuid} abort_fallback"
                                    )
                                    self.logger.error(
                                        f"FALLBACK_ABORTED | side=BUY ticker={ticker} "
                                        f"uuid={order_uuid} reason=cancel_failed_status_unknown"
                                    )
                                return None

                            executed_volume = float(order_info.get('executed_volume', 0) or 0)
                            remaining_volume = float(order_info.get('remaining_volume', 0) or 0)
                            state = str(order_info.get('state', '') or '').lower()

                            # 완전 체결(or 잔량 0) 처리
                            if state == 'done' or (remaining_volume <= 0 and executed_volume > 0):
                                avg_price = float(order_info.get('avg_buy_price', 0) or 0)
                                paid_fee = float(order_info.get('paid_fee', 0) or 0)

                                if avg_price == 0:
                                    avg_price = bid_price
                                    self.logger.warning(f"  ⚠️  avg_buy_price 없음, bid_price 사용: {avg_price:,.0f}원")

                                self.logger.info(
                                    f"LIMIT_ORDER_FILLED | side=BUY ticker={ticker} "
                                    f"uuid={order_uuid} executed={executed_volume:.8f} state={state or 'unknown'}"
                                )
                                self.logger.info(f"  ✅ 지정가 완전체결: {avg_price:,.0f}원 × {executed_volume:.8f}")

                                return {
                                    'price': avg_price,
                                    'amount': executed_volume,
                                    'total_krw': invest_amount,
                                    'fee': paid_fee,
                                    'uuid': order_uuid
                                }

                            # 부분 체결
                            if executed_volume > 0:
                                self.logger.info(
                                    f"LIMIT_ORDER_PARTIAL | side=BUY ticker={ticker} "
                                    f"uuid={order_uuid} executed={executed_volume:.8f} remaining={remaining_volume:.8f}"
                                )
                                self.logger.warning(f"  ⚠️  부분체결: {executed_volume:.8f} / {buy_amount:.8f}")

                                executed_value = executed_volume * bid_price
                                remaining_value = max(0.0, invest_amount - executed_value)

                                cancel_ok = self._try_cancel_limit(order_uuid, side="BUY", ticker=ticker, retries=3)
                                self.logger.info(
                                    f"LIMIT_ORDER_TIMEOUT_CANCEL_RESULT | side=BUY ticker={ticker} "
                                    f"uuid={order_uuid} ok={cancel_ok} reason=partial_fill"
                                )
                                if not cancel_ok:
                                    self.logger.error(
                                        f"FALLBACK_ABORTED | side=BUY ticker={ticker} "
                                        f"uuid={order_uuid} reason=cancel_failed_partial_fill"
                                    )
                                    return None
                                self.logger.info(f"Cancel confirmation: {cancel_ok}")
                                time.sleep(0.3)

                                if remaining_value >= min_trade_amount:
                                    self.logger.info(
                                        f"LIMIT_ORDER_TIMEOUT_FALLBACK_MARKET | side=BUY ticker={ticker} "
                                        f"uuid={order_uuid} reason=partial_fill remaining_krw={remaining_value:,.0f}"
                                    )
                                    self.logger.info("Fallback to market triggered.")
                                    self.logger.info(f"  ↪️  남은 {remaining_value:,.0f}원 시장가 처리")

                                    market_result = self.upbit.buy_market_order(ticker, remaining_value)
                                    if market_result and 'uuid' in market_result:
                                        time.sleep(0.5)
                                        market_order = self._safe_get_order(market_result['uuid'])

                                        if market_order:
                                            market_volume = float(market_order.get('executed_volume', 0) or 0)
                                            market_price = float(market_order.get('avg_buy_price', current_price) or current_price)
                                            market_fee = float(market_order.get('paid_fee', 0) or 0)

                                            total_volume = executed_volume + market_volume
                                            if total_volume <= 0:
                                                return None
                                            total_fees = float(order_info.get('paid_fee', 0) or 0) + market_fee
                                            avg_price = (
                                                (executed_volume * bid_price) + (market_volume * market_price)
                                            ) / total_volume

                                            self.logger.info(f"  ✅ 부분+시장가 체결완료: 평단 {avg_price:,.0f}원")

                                            return {
                                                'price': avg_price,
                                                'amount': total_volume,
                                                'total_krw': invest_amount,
                                                'fee': total_fees,
                                                'uuid': order_uuid
                                            }

                                avg_price = float(order_info.get('avg_buy_price', bid_price) or bid_price)
                                paid_fee = float(order_info.get('paid_fee', 0) or 0)
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

                            sleep_left = deadline - time.time()
                            if sleep_left > 0:
                                time.sleep(min(poll_interval, sleep_left))

                        # 타임아웃: 취소 성공 확인 후에만 시장가 폴백
                        cancel_ok = self._try_cancel_limit(order_uuid, side="BUY", ticker=ticker, retries=3)
                        self.logger.info(
                            f"LIMIT_ORDER_TIMEOUT_CANCEL_RESULT | side=BUY ticker={ticker} "
                            f"uuid={order_uuid} ok={cancel_ok} reason=timeout"
                        )
                        if not cancel_ok:
                            self.logger.error(
                                f"FALLBACK_ABORTED | side=BUY ticker={ticker} "
                                f"uuid={order_uuid} reason=cancel_failed_timeout"
                            )
                            return None
                        self.logger.info(
                            f"LIMIT_ORDER_TIMEOUT_FALLBACK_MARKET | side=BUY ticker={ticker} "
                            f"uuid={order_uuid} reason=timeout"
                        )
                        self.logger.info("Fallback to market triggered.")
                else:
                    self.logger.warning(f"LIMIT_ORDERBOOK_PARSE_FAIL | side=BUY ticker={ticker}")
                    self.logger.warning(f"{ticker} orderbook parse 실패, 시장가 폴백")
                    self.logger.info(
                        f"LIMIT_ORDER_TIMEOUT_FALLBACK_MARKET | side=BUY ticker={ticker} "
                        "uuid=none reason=orderbook_parse_fail"
                    )
                    self.logger.info("Fallback to market triggered.")
            
            # 2단계: 시장가 주문 (폴백 또는 기본)
            result = self.upbit.buy_market_order(ticker, invest_amount)
            
            if result is None:
                self.logger.warning(f"⚠️  {ticker} 매수 주문 실패")
                return None
            
            time.sleep(0.5)
            
            # UUID로 정확한 체결 정보 확인
            if 'uuid' in result:
                order_info = self._safe_get_order(result['uuid'])
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
                if isinstance(orderbook, list) and len(orderbook) > 0:
                    orderbook = orderbook[0]

                if isinstance(orderbook, dict) and "orderbook_units" in orderbook and orderbook["orderbook_units"]:
                    # 매도 1호가 (최선 매도가)
                    ask_price = orderbook['orderbook_units'][0]['ask_price']
                    
                    self.logger.info(
                        f"LIMIT_ORDER_ATTEMPT | side=SELL ticker={ticker} price={ask_price:,.0f} qty={sell_amount:.8f}"
                    )
                    
                    # 지정가 주문
                    result = self.upbit.sell_limit_order(ticker, ask_price, sell_amount)
                    
                    if result and 'uuid' in result:
                        order_uuid = result['uuid']
                        self.logger.info(
                            f"LIMIT_ORDER_PLACED | side=SELL ticker={ticker} "
                            f"uuid={order_uuid} price={ask_price:,.0f} qty={sell_amount:.8f}"
                        )
                        self.logger.info("Limit order placed, waiting fill...")
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
                                cancel_result = self.upbit.cancel_order(order_uuid)
                                self.logger.info(
                                    f"LIMIT_ORDER_CANCEL_RESULT | side=SELL ticker={ticker} "
                                    f"uuid={order_uuid} cancelled={bool(cancel_result)}"
                                )
                                self.logger.info(f"Cancel confirmation: {bool(cancel_result)}")
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
                                
                                self.logger.info(
                                    f"LIMIT_ORDER_TIMEOUT_FALLBACK_MARKET | side=SELL ticker={ticker} "
                                    f"uuid={order_uuid} reason=partial_fill remaining_qty={remaining_balance:.8f}"
                                )
                                self.logger.info("Fallback to market triggered.")
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
                            self.logger.info(
                                f"LIMIT_ORDER_TIMEOUT_FALLBACK_MARKET | side=SELL ticker={ticker} "
                                f"uuid={order_uuid} reason=not_filled"
                            )
                            self.logger.info("Fallback to market triggered.")
                            cancel_result = self.upbit.cancel_order(order_uuid)
                            self.logger.info(
                                f"LIMIT_ORDER_CANCEL_RESULT | side=SELL ticker={ticker} "
                                f"uuid={order_uuid} cancelled={bool(cancel_result)}"
                            )
                            self.logger.info(f"Cancel confirmation: {bool(cancel_result)}")
                            time.sleep(0.3)
                else:
                    self.logger.warning(f"LIMIT_ORDERBOOK_PARSE_FAIL | side=SELL ticker={ticker}")
                    self.logger.warning(f"{ticker} orderbook parse 실패, 시장가 폴백")
                    self.logger.info(
                        f"LIMIT_ORDER_TIMEOUT_FALLBACK_MARKET | side=SELL ticker={ticker} "
                        "uuid=none reason=orderbook_parse_fail"
                    )
                    self.logger.info("Fallback to market triggered.")
            
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
            if self.upbit is None:
                return 0.0
            value = self.upbit.get_balance(currency)
            if value is None:
                return 0.0
            return float(value)
        except Exception as e:
            self.logger.log_error("잔고 조회 오류", e)
            return 0.0
    
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
            if self.upbit is None:
                return 0.0
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
