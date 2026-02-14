"""
코인 선정 모듈 - 레짐 기반 고정 유니버스 선정
"""

import pyupbit


class CoinSelector:
    def __init__(self, config, logger, engine=None):
        self.config = config
        self.logger = logger
        self.engine = engine

        coin_cfg = config.get("coin_selection", {}) or {}
        trading_cfg = config.get("trading", {}) or {}
        strategy_cfg = config.get("strategy", {}) or {}

        self.min_volume_krw = float(coin_cfg.get("min_volume_krw", 3_000_000_000))
        self.min_volatility = float(coin_cfg.get("min_volatility", 1.0))
        self.max_volatility = float(coin_cfg.get("max_volatility", 20.0))
        self.min_quality_score = float(coin_cfg.get("min_quality_score", 8.0))
        self.max_spread_pct = float(trading_cfg.get("max_spread_percent", 0.5))
        self.min_orderbook_depth = float(trading_cfg.get("min_orderbook_depth_krw", 1_500_000))

        self.excluded_coins = {str(x).upper() for x in coin_cfg.get("excluded_coins", []) if x}
        self.fixed_tickers = self._build_universe(coin_cfg.get("fixed_tickers"))
        self.default_universe = self._build_universe(strategy_cfg.get("universe"))
        self.default_max_positions = int(strategy_cfg.get("max_positions", trading_cfg.get("max_coins", 3)))

    @staticmethod
    def _normalize_ticker(value):
        text = str(value or "").upper().strip()
        if not text:
            return ""
        if text.startswith("KRW-"):
            return text
        if "-" in text:
            return f"KRW-{text.split('-')[-1]}"
        return f"KRW-{text}"

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    def _build_universe(self, raw_universe):
        values = raw_universe if raw_universe else ["SOL", "DOGE", "ADA"]
        out = []
        for value in values:
            ticker = self._normalize_ticker(value)
            if ticker and ticker not in out:
                out.append(ticker)
        return out

    def _symbol(self, ticker):
        text = str(ticker or "").upper()
        return text.split("-")[-1] if "-" in text else text

    def _is_orderbook_healthy(self, ticker):
        try:
            orderbook = pyupbit.get_orderbook(ticker)
            if isinstance(orderbook, list) and orderbook:
                orderbook = orderbook[0]
            if not isinstance(orderbook, dict):
                return False, {"reason": "호가없음"}
            units = orderbook.get("orderbook_units") or []
            if not units:
                return False, {"reason": "호가없음"}

            unit = units[0]
            ask_price = self._to_float(unit.get("ask_price", 0))
            bid_price = self._to_float(unit.get("bid_price", 0))
            ask_size = self._to_float(unit.get("ask_size", 0))
            bid_size = self._to_float(unit.get("bid_size", 0))

            if ask_price <= 0 or bid_price <= 0:
                return False, {"reason": "호가가격이상"}

            spread_pct = ((ask_price - bid_price) / bid_price) * 100
            ask_depth_krw = ask_price * ask_size
            bid_depth_krw = bid_price * bid_size

            details = {
                "spread_pct": float(spread_pct),
                "ask_depth_krw": float(ask_depth_krw),
                "bid_depth_krw": float(bid_depth_krw),
                "depth_min_krw": float(min(ask_depth_krw, bid_depth_krw)),
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

    def _fetch_day_quality(self, ticker):
        try:
            df_day = pyupbit.get_ohlcv(ticker, interval="day", count=2)
            if df_day is None or len(df_day) < 1:
                return None
            row = df_day.iloc[-1]
            price = self._to_float(row.get("close", 0), 0)
            volume_krw = self._to_float(row.get("value", 0), 0)
            high = self._to_float(row.get("high", 0), 0)
            low = self._to_float(row.get("low", 0), 0)
            if price <= 0 or volume_krw <= 0 or low <= 0:
                return None
            volatility = ((high - low) / low) * 100
            return {
                "price": float(price),
                "volume_krw": float(volume_krw),
                "volatility": float(volatility),
            }
        except Exception:
            return None

    def get_top_coins(self, max_coins=3):
        """스펙 기반 유니버스에서 거래 가능 상위 종목 선정."""
        self.logger.info("🔍 레짐 기반 종목 선정 시작...")

        try:
            if self.engine and hasattr(self.engine, "update_global_regime"):
                regime, _ = self.engine.update_global_regime(force=False)
            else:
                regime = "RANGE"

            universe = list(self.engine.get_universe()) if self.engine and hasattr(self.engine, "get_universe") else list(self.default_universe)
            limit = max(1, min(int(max_coins or 1), int(self.default_max_positions)))
            fixed = [t for t in self.fixed_tickers if self._symbol(t) not in self.excluded_coins]

            if regime == "BEAR":
                self.logger.info("🛑 글로벌 레짐 BEAR: 신규 진입 대상 없음(현물 대기)")
                return list(fixed)

            candidates = []
            for ticker in universe:
                symbol = self._symbol(ticker)
                if symbol in self.excluded_coins:
                    continue

                day = self._fetch_day_quality(ticker)
                if not day:
                    continue
                if day["volume_krw"] < self.min_volume_krw:
                    continue
                if day["volatility"] < self.min_volatility or day["volatility"] > self.max_volatility:
                    continue

                if not self.engine or not hasattr(self.engine, "analyze_symbol"):
                    continue
                state = self.engine.analyze_symbol(ticker)
                if not state:
                    continue

                strategy = self.engine.select_strategy(state, regime)
                if not strategy:
                    continue

                if state.get("middle_zone"):
                    continue
                if not state.get("volatility_ok"):
                    continue

                ok_ob, ob = self._is_orderbook_healthy(ticker)
                if not ok_ob:
                    continue

                quality_score = self._to_float(state.get("quality_score", 0), 0)
                if quality_score < self.min_quality_score:
                    continue

                spread_bonus = max(0.0, (self.max_spread_pct - self._to_float(ob.get("spread_pct", 0), 0))) * 2.0
                depth_bonus = min(6.0, self._to_float(ob.get("depth_min_krw", 0), 0) / max(self.min_orderbook_depth, 1) * 2.0)

                score = quality_score + spread_bonus + depth_bonus
                candidates.append(
                    {
                        "ticker": ticker,
                        "symbol": symbol,
                        "strategy": strategy,
                        "score": float(score),
                        "quality": float(quality_score),
                        "rsi": self._to_float(state.get("rsi", 0), 0),
                        "atr_pct": self._to_float(state.get("atr_pct", 0), 0),
                        "range_position": self._to_float(state.get("range_position", 0.5), 0.5),
                        "volume_krw": float(day["volume_krw"]),
                        "volatility": float(day["volatility"]),
                        "spread_pct": self._to_float(ob.get("spread_pct", 0), 0),
                    }
                )

            if not candidates:
                if fixed:
                    self.logger.info("ℹ️ 레짐 조건 종목 없음, 고정 종목만 유지합니다.")
                    return list(fixed)
                self.logger.warning("⚠️ 레짐 조건을 통과한 종목이 없습니다.")
                return []

            candidates.sort(key=lambda x: x["score"], reverse=True)
            top = candidates[:limit]
            selected = []
            for ticker in fixed:
                if ticker not in selected:
                    selected.append(ticker)
            for row in top:
                if row["ticker"] not in selected:
                    selected.append(row["ticker"])

            self.logger.info("=" * 80)
            self.logger.info(
                f"🏆 선정 결과 | 레짐={regime} | 대상={len(selected)}개 "
                f"(고정 {len(fixed)} + 조건통과 {len(top)})"
            )
            if fixed:
                fixed_symbols = ", ".join(self._symbol(t) for t in fixed)
                self.logger.info(f"  고정 종목: {fixed_symbols}")
            for idx, row in enumerate(top, start=1):
                self.logger.info(
                    f"  [{idx}] {row['symbol']} | 전략:{row['strategy']} | "
                    f"점수:{row['score']:.1f} (q:{row['quality']:.1f}) | "
                    f"RSI:{row['rsi']:.1f} | ATR:{row['atr_pct']:.2f}% | "
                    f"위치:{row['range_position']:.2f} | "
                    f"거래량:{row['volume_krw']/100000000:.0f}억 | 스프레드:{row['spread_pct']:.2f}%"
                )
            self.logger.info("=" * 80)

            return selected
        except Exception as e:
            self.logger.log_error("코인 선정 중 오류 발생", e)
            return []
