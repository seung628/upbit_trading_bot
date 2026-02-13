"""
코인 선정 모듈 - 단기 추세/체결 품질 기반 후보 선정
"""

import pyupbit
import pandas as pd
import numpy as np


class CoinSelector:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

        coin_cfg = config.get('coin_selection', {}) or {}
        trading_cfg = config.get('trading', {}) or {}
        indicators = config.get('indicators', {}) or {}
        strategy_cfg = config.get('strategy', {}) or {}

        self.min_volume = float(coin_cfg.get('min_volume_krw', 10_000_000_000))
        self.min_volatility = float(coin_cfg.get('min_volatility', 3))
        self.max_volatility = float(coin_cfg.get('max_volatility', 15))
        self.excluded_coins = [str(x).upper() for x in coin_cfg.get('excluded_coins', []) if x]

        # RSI 진입 범위는 매수 엔진과 동일 기준 사용
        try:
            self.rsi_buy_min = float(indicators.get('rsi_buy_min', 50))
        except Exception:
            self.rsi_buy_min = 50.0
        try:
            self.rsi_buy_max = float(indicators.get('rsi_buy_max', 70))
        except Exception:
            self.rsi_buy_max = 70.0

        # 종목 선정 품질 옵션
        try:
            self.shortlist_multiplier = max(2, int(coin_cfg.get('selection_shortlist_multiplier', 4)))
        except Exception:
            self.shortlist_multiplier = 4
        val = coin_cfg.get('orderbook_filter_enabled', True)
        self.orderbook_filter_enabled = True if val is None else bool(val)
        val = coin_cfg.get('require_trend_alignment', False)
        self.require_trend_alignment = False if val is None else bool(val)

        # 주문 체결 품질 필터(선정 단계에서 사전 적용)
        try:
            self.max_spread_pct = float(trading_cfg.get('max_spread_percent', 0.5))
        except Exception:
            self.max_spread_pct = 0.5
        try:
            self.min_orderbook_depth = float(trading_cfg.get('min_orderbook_depth_krw', 5_000_000))
        except Exception:
            self.min_orderbook_depth = 5_000_000.0

        # 전략 추세 파라미터 공유
        try:
            self.entry_ma_fast = int(strategy_cfg.get('entry_ma_fast', 20))
        except Exception:
            self.entry_ma_fast = 20
        try:
            self.entry_ma_slow = int(strategy_cfg.get('entry_ma_slow', 60))
        except Exception:
            self.entry_ma_slow = 60
        try:
            self.entry_breakout_lookback = int(strategy_cfg.get('entry_breakout_lookback', 20))
        except Exception:
            self.entry_breakout_lookback = 20

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    def _calculate_rsi(self, close_series, period=14):
        delta = close_series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def _is_orderbook_healthy(self, ticker):
        """호가 스프레드/깊이 체크."""
        try:
            orderbook = pyupbit.get_orderbook(ticker)
            if not orderbook or 'orderbook_units' not in orderbook or not orderbook['orderbook_units']:
                return False, {"reason": "호가없음"}

            unit = orderbook['orderbook_units'][0]
            ask_price = self._to_float(unit.get('ask_price', 0))
            bid_price = self._to_float(unit.get('bid_price', 0))
            ask_size = self._to_float(unit.get('ask_size', 0))
            bid_size = self._to_float(unit.get('bid_size', 0))

            if ask_price <= 0 or bid_price <= 0:
                return False, {"reason": "호가가격0"}

            spread_pct = ((ask_price - bid_price) / bid_price) * 100
            ask_depth_krw = ask_price * ask_size
            bid_depth_krw = bid_price * bid_size
            depth_min = min(ask_depth_krw, bid_depth_krw)

            details = {
                "spread_pct": float(spread_pct),
                "ask_depth_krw": float(ask_depth_krw),
                "bid_depth_krw": float(bid_depth_krw),
                "depth_min_krw": float(depth_min),
            }

            if spread_pct > self.max_spread_pct:
                details["reason"] = f"스프레드과다({spread_pct:.2f}%)"
                return False, details
            if ask_depth_krw < self.min_orderbook_depth or bid_depth_krw < self.min_orderbook_depth:
                details["reason"] = "호가잔량부족"
                return False, details

            details["reason"] = "ok"
            return True, details
        except Exception as e:
            return False, {"reason": f"호가예외:{type(e).__name__}"}

    def _calculate_score(
        self,
        volume_krw,
        volatility,
        volume_trend,
        bb_width,
        rsi,
        rsi_slope=0.0,
        trend_alignment=False,
        trend_strength=0.0,
        breakout_distance_pct=0.0,
    ):
        """종목 점수 계산."""

        # 거래량 점수 (로그 스케일)
        volume_score = np.log10(max(volume_krw, 1_000_000_000) / 1_000_000_000) * 10

        # 변동성 점수 (과도한 변동성/저변동성 회피)
        if 4 <= volatility <= 9:
            volatility_score = 28
        elif 3 <= volatility < 4:
            volatility_score = 16
        elif 9 < volatility <= 12:
            volatility_score = 14
        else:
            volatility_score = 6

        # 거래량 추세
        if volume_trend >= 1.4:
            trend_vol_score = 22
        elif volume_trend >= 1.15:
            trend_vol_score = 14
        elif volume_trend >= 1.0:
            trend_vol_score = 7
        else:
            trend_vol_score = 0

        # BB 폭 (너무 좁거나 과도한 구간 회피)
        if 0.8 <= bb_width <= 2.8:
            bb_score = 16
        elif 0.5 <= bb_width < 0.8:
            bb_score = 9
        elif 2.8 < bb_width <= 4.0:
            bb_score = 9
        else:
            bb_score = 2

        # RSI 점수 (모멘텀 구간 선호)
        if pd.isna(rsi):
            rsi_score = -8
        elif rsi < 40:
            rsi_score = -6
        elif rsi < 45:
            rsi_score = 2
        elif rsi < 50:
            rsi_score = 8
        elif rsi < 65:
            rsi_score = 20
        elif rsi < 72:
            rsi_score = 12
        else:
            rsi_score = 2

        # RSI 기울기(10분)
        if rsi_slope >= 5:
            rsi_slope_score = 10
        elif rsi_slope >= 2:
            rsi_slope_score = 6
        elif rsi_slope > 0:
            rsi_slope_score = 3
        else:
            rsi_slope_score = -1

        # 추세 정렬 점수
        if trend_alignment:
            trend_align_score = 18
        elif trend_strength > 0:
            trend_align_score = 4
        else:
            trend_align_score = -12

        # 돌파 근접도 (너무 멀리 이탈한 추격매수 회피)
        if -0.5 <= breakout_distance_pct <= 0.8:
            breakout_score = 10
        elif -1.5 <= breakout_distance_pct < -0.5:
            breakout_score = 6
        elif breakout_distance_pct < -3.0:
            breakout_score = -4
        elif breakout_distance_pct > 2.5:
            breakout_score = -8
        else:
            breakout_score = 2

        return (
            volume_score
            + volatility_score
            + trend_vol_score
            + bb_score
            + rsi_score
            + rsi_slope_score
            + trend_align_score
            + breakout_score
        )

    def get_top_coins(self, max_coins=3):
        """단타 거래에 적합한 코인 선정."""

        self.logger.info("🔍 거래 적합 코인 분석 시작...")

        try:
            tickers = pyupbit.get_tickers(fiat="KRW")
            candidates = []
            candidates_in_rsi_range = []

            for ticker in tickers:
                try:
                    symbol = ticker.replace("KRW-", "").upper()
                    if symbol in self.excluded_coins:
                        continue

                    # 1) 일봉 요건 필터
                    df_day = pyupbit.get_ohlcv(ticker, interval="day", count=1)
                    if df_day is None or len(df_day) < 1:
                        continue

                    current_price = self._to_float(df_day['close'].iloc[-1], 0)
                    volume_krw = self._to_float(df_day['value'].iloc[-1], 0)
                    if current_price <= 0 or volume_krw <= 0:
                        continue

                    high_24h = self._to_float(df_day['high'].iloc[-1], 0)
                    low_24h = self._to_float(df_day['low'].iloc[-1], 0)
                    if low_24h <= 0:
                        continue
                    volatility = ((high_24h - low_24h) / low_24h) * 100

                    if volume_krw < self.min_volume:
                        continue
                    if volatility < self.min_volatility or volatility > self.max_volatility:
                        continue

                    # 2) 1분봉 기반 모멘텀/추세
                    need_count = max(220, self.entry_ma_slow + 80, self.entry_breakout_lookback + 40)
                    df_min = pyupbit.get_ohlcv(ticker, interval="minute1", count=need_count)
                    if df_min is None or len(df_min) < max(160, self.entry_ma_slow + 5):
                        continue

                    close = df_min['close']
                    vol = df_min['volume']

                    recent_volume = vol.tail(60).mean()
                    older_volume = vol.iloc[-180:-60].mean()
                    volume_trend = (recent_volume / older_volume) if (older_volume and older_volume > 0) else 1.0

                    df_min['ma20'] = close.rolling(20).mean()
                    df_min['std20'] = close.rolling(20).std()
                    df_min['bb_width'] = (df_min['std20'] / df_min['ma20']) * 100
                    bb_width = self._to_float(df_min['bb_width'].tail(120).mean(), 0)

                    rsi = self._calculate_rsi(close, period=14)
                    if len(rsi) < 15:
                        continue
                    current_rsi = self._to_float(rsi.iloc[-2], np.nan)  # 확정봉 기준
                    try:
                        rsi_slope = self._to_float(current_rsi - rsi.iloc[-12], 0.0)
                    except Exception:
                        rsi_slope = 0.0
                    rsi_in_range = (not pd.isna(current_rsi)) and (self.rsi_buy_min <= current_rsi < self.rsi_buy_max)

                    df_min['ema_fast'] = close.ewm(span=self.entry_ma_fast, adjust=False).mean()
                    df_min['ema_slow'] = close.ewm(span=self.entry_ma_slow, adjust=False).mean()
                    cur = df_min.iloc[-2]
                    prev = df_min.iloc[-3]
                    close_c = self._to_float(cur.get('close', 0), 0)
                    ema_fast_c = self._to_float(cur.get('ema_fast', 0), 0)
                    ema_slow_c = self._to_float(cur.get('ema_slow', 0), 0)
                    ema_fast_prev = self._to_float(prev.get('ema_fast', 0), 0)

                    trend_alignment = bool(
                        close_c > ema_fast_c > ema_slow_c
                        and ema_fast_c >= ema_fast_prev
                    )
                    trend_strength = ((ema_fast_c / ema_slow_c) - 1) * 100 if ema_slow_c > 0 else 0.0

                    breakout_window = df_min['high'].iloc[-(self.entry_breakout_lookback + 2):-2]
                    if breakout_window is None or len(breakout_window) < self.entry_breakout_lookback:
                        continue
                    breakout_base = self._to_float(breakout_window.max(), 0)
                    if breakout_base <= 0:
                        continue
                    breakout_distance_pct = ((close_c / breakout_base) - 1) * 100

                    if self.require_trend_alignment and not trend_alignment:
                        continue

                    score = self._calculate_score(
                        volume_krw=volume_krw,
                        volatility=volatility,
                        volume_trend=volume_trend,
                        bb_width=bb_width,
                        rsi=current_rsi,
                        rsi_slope=rsi_slope,
                        trend_alignment=trend_alignment,
                        trend_strength=trend_strength,
                        breakout_distance_pct=breakout_distance_pct,
                    )

                    if rsi_in_range:
                        score += 15
                    else:
                        score -= 10

                    row = {
                        'ticker': ticker,
                        'name': symbol,
                        'price': current_price,
                        'volume_krw': volume_krw,
                        'volatility': volatility,
                        'volume_trend': self._to_float(volume_trend, 1.0),
                        'bb_width': bb_width,
                        'rsi': current_rsi,
                        'rsi_in_range': rsi_in_range,
                        'trend_alignment': trend_alignment,
                        'trend_strength': self._to_float(trend_strength, 0.0),
                        'breakout_distance_pct': self._to_float(breakout_distance_pct, 0.0),
                        'score': self._to_float(score, 0.0),
                    }
                    candidates.append(row)
                    if rsi_in_range:
                        candidates_in_rsi_range.append(row)
                except Exception:
                    continue

            if not candidates:
                self.logger.warning("⚠️  조건에 맞는 코인이 없습니다. (데이터 수집 실패 가능)")
                return []

            # 1차 점수 상위 후보 추림
            candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)
            shortlist_size = max(max_coins, max_coins * self.shortlist_multiplier)
            shortlist = candidates[:shortlist_size]

            # 2차 호가 품질 필터
            orderbook_blocked = 0
            if self.orderbook_filter_enabled:
                filtered = []
                for row in shortlist:
                    ok, ob = self._is_orderbook_healthy(row['ticker'])
                    if not ok:
                        orderbook_blocked += 1
                        continue

                    spread = self._to_float(ob.get('spread_pct', 0), 0)
                    spread_gain = max(0.0, self.max_spread_pct - spread) / max(self.max_spread_pct, 0.01)

                    row2 = dict(row)
                    row2['spread_pct'] = spread
                    row2['orderbook_depth_min_krw'] = self._to_float(ob.get('depth_min_krw', 0), 0)
                    row2['score'] = row2['score'] + (spread_gain * 3.0)
                    filtered.append(row2)

                if filtered:
                    shortlist = sorted(filtered, key=lambda x: x['score'], reverse=True)
                else:
                    self.logger.info("ℹ️ 호가 품질 필터를 통과한 코인이 없어 점수 상위 후보를 사용합니다.")

            # 3차 RSI 우선 정책
            pool_in_range = [x for x in shortlist if x.get('rsi_in_range')]
            selected_pool = pool_in_range if len(pool_in_range) >= max_coins else shortlist

            if selected_pool is shortlist and pool_in_range:
                self.logger.info(
                    f"ℹ️ RSI {self.rsi_buy_min:.0f}~{self.rsi_buy_max:.0f} 후보가 부족하여 "
                    f"일부 범위 밖 코인도 포함해 선정합니다. (in-range {len(pool_in_range)}/{len(shortlist)})"
                )
            elif selected_pool is shortlist and not pool_in_range:
                self.logger.info(
                    f"ℹ️ RSI {self.rsi_buy_min:.0f}~{self.rsi_buy_max:.0f} 후보가 없어 RSI 조건을 완화하여 선정합니다."
                )

            top_coins = sorted(selected_pool, key=lambda x: x['score'], reverse=True)[:max_coins]

            self.logger.info("=" * 80)
            self.logger.info("🏆 선정된 거래 코인")
            if self.excluded_coins:
                self.logger.info(f"   (제외 코인: {', '.join(self.excluded_coins)})")
            if self.orderbook_filter_enabled:
                self.logger.info(f"   (호가 품질 필터 제외: {orderbook_blocked}개)")
            self.logger.info("=" * 80)

            for i, row in enumerate(top_coins, start=1):
                trend_tag = "T✅" if row.get('trend_alignment') else "T❌"
                spread_text = (
                    f"{row['spread_pct']:.2f}%"
                    if row.get('spread_pct') is not None
                    else "N/A"
                )
                self.logger.info(
                    f"  [{i}] {row['name']} | "
                    f"가격: {row['price']:,.0f}원 | "
                    f"거래량: {row['volume_krw']/100000000:.0f}억 | "
                    f"변동성: {row['volatility']:.2f}% | "
                    f"RSI: {row['rsi']:.1f}{'✅' if row.get('rsi_in_range') else ''} | "
                    f"{trend_tag} | "
                    f"스프레드: {spread_text} | "
                    f"점수: {row['score']:.1f}"
                )

            self.logger.info("=" * 80)
            return [x['ticker'] for x in top_coins]
        except Exception as e:
            self.logger.log_error("코인 선정 중 오류 발생", e)
            return []
