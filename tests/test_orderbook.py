import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from trading_engine import TradingEngine


class FakeLogger:
    def __init__(self):
        self.records = []

    def info(self, message):
        self.records.append(("info", str(message)))

    def warning(self, message):
        self.records.append(("warning", str(message)))

    def error(self, message):
        self.records.append(("error", str(message)))

    def debug(self, message):
        self.records.append(("debug", str(message)))

    def log_error(self, message, exception=None):
        if exception is not None:
            self.records.append(("error", f"{message}: {exception}"))
        else:
            self.records.append(("error", str(message)))

    def log_decision(self, event, payload=None):
        self.records.append(("decision", f"{event}:{payload or {}}"))

    def has_info(self, token):
        return any(level == "info" and token in msg for level, msg in self.records)

    def has_warning(self, token):
        return any(level == "warning" and token in msg for level, msg in self.records)

    def has_error(self, token):
        return any(level == "error" and token in msg for level, msg in self.records)


class FakeStats:
    def __init__(self):
        self.positions = {}

    def save_positions(self):
        return None

    def update_position_highest(self, ticker, highest_price):
        if ticker in self.positions:
            self.positions[ticker]["highest_price"] = highest_price
        return None


class DummyUpbit:
    def __init__(self):
        self.buy_limit_calls = []
        self.buy_market_calls = []

    def buy_limit_order(self, ticker, price, amount):
        self.buy_limit_calls.append((ticker, float(price), float(amount)))
        return {"uuid": "buy-uuid"}

    def buy_market_order(self, ticker, amount_krw):
        self.buy_market_calls.append((ticker, float(amount_krw)))
        return {"uuid": "market-uuid"}

    def get_order(self, uuid):
        if uuid == "buy-uuid":
            return {
                "state": "done",
                "executed_volume": "1.00000000",
                "avg_buy_price": "100.0",
                "paid_fee": "0.05",
            }
        return {
            "state": "done",
            "executed_volume": "0.0",
            "avg_buy_price": "0.0",
            "paid_fee": "0.0",
        }


class DummyUpbitUnknownStatus:
    def __init__(self, cancel_returns=None):
        self.buy_limit_calls = []
        self.buy_market_calls = []
        self.cancel_calls = []
        self.cancel_returns = list(cancel_returns or [])

    def buy_limit_order(self, ticker, price, amount):
        self.buy_limit_calls.append((ticker, float(price), float(amount)))
        return {"uuid": "buy-uuid"}

    def buy_market_order(self, ticker, amount_krw):
        self.buy_market_calls.append((ticker, float(amount_krw)))
        return {"uuid": "market-uuid"}

    def get_order(self, uuid):
        if uuid == "buy-uuid":
            return None
        return {
            "state": "done",
            "executed_volume": "0.0",
            "avg_buy_price": "0.0",
            "paid_fee": "0.0",
        }

    def cancel_order(self, uuid):
        self.cancel_calls.append(uuid)
        if self.cancel_returns:
            return self.cancel_returns.pop(0)
        return None


def make_config(min_depth=1000):
    return {
        "trading": {
            "max_total_investment": 500000,
            "max_spread_percent": 1.0,
            "min_orderbook_depth_krw": float(min_depth),
            "order_type": "limit_with_fallback",
            "limit_order_wait_seconds": 0,
            "min_trade_amount": 5500,
            "max_coins": 2,
        },
        "strategy": {
            "entry_interval": "minute20",
            "signal_candle_minutes": 20,
            "analysis_lookback": 240,
            "mode": "regime_spec",
            "symbol_strategy_map": {
                "SOL": {"strategy": "SOL_TREND", "regimes": ["BULL"]},
                "XRP": {"strategy": "XRP_FLOW", "regimes": ["BULL", "RANGE"]},
                "ADA": {"strategy": "ADA_RANGE", "regimes": ["RANGE"]},
            },
            "regime_reference": "KRW-BTC",
            "regime_check_minutes": 20,
            "regime_confirm_count": 3,
            "regime_min_hold_minutes": 0,
            "max_positions": 2,
            "entry_time_filter": {"start_hour": 2, "end_hour": 6},
            "volatility_tr_atr_max": 3.0,
            "btc_filter": {"enabled": True, "ticker": "KRW-BTC", "ema_period": 50},
            "universe": ["SOL", "XRP", "ADA"],
        },
        "risk_management": {
            "atr_period": 14,
            "stop_loss_pct": -1.8,
            "trailing_stop_pct": 1.0,
            "trailing_activation_pct": 2.0,
            "min_hold_minutes": 20,
            "max_hold_minutes": 360,
            "time_stop_candles": 10,
            "risk_per_trade_pct": 0.4,
        },
        "indicators": {"rsi_period": 14, "bb_period": 20, "bb_std": 2.0},
    }


class TradingEngineOrderbookTests(unittest.TestCase):
    @staticmethod
    def _xrp_state(**overrides):
        base = {
            "ticker": "KRW-XRP",
            "candle_ts": "2026-02-15 00:00:00",
            "close": 100.0,
            "rsi": 60.0,
            "atr": 1.5,
            "atr_pct": 1.5,
            "tr_atr_ratio": 1.0,
            "volume_ratio": 1.4,
            "range_position": 0.5,
            "middle_zone": False,
            "breakout_level": 98.0,
            "swing_low": 90.0,
            "swing_high": 110.0,
            "quality_score": 20.0,
            "xrp_trend_ok": True,
            "xrp_pullback_to_ema20": True,
        }
        base.update(overrides)
        return base

    def test_limit_with_fallback_normalizes_orderbook_list_and_dict(self):
        for orderbook_payload in (
            [{"orderbook_units": [{"bid_price": 100.0, "ask_price": 101.0}]}],
            {"orderbook_units": [{"bid_price": 100.0, "ask_price": 101.0}]},
        ):
            with self.subTest(payload_type=type(orderbook_payload).__name__):
                logger = FakeLogger()
                engine = TradingEngine(make_config(), logger, FakeStats())
                engine.upbit = DummyUpbit()

                with patch("trading_engine.pyupbit.get_current_price", return_value=100.0):
                    with patch("trading_engine.pyupbit.get_orderbook", return_value=orderbook_payload):
                        result = engine.execute_buy("KRW-TEST", 10000)

                self.assertIsNotNone(result)
                self.assertEqual(len(engine.upbit.buy_limit_calls), 1)
                self.assertEqual(len(engine.upbit.buy_market_calls), 0)
                self.assertEqual(result.get("uuid"), "buy-uuid")

    def test_liquidity_check_uses_top5_aggregation_and_logs_low_liquidity(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(min_depth=1000), logger, FakeStats())

        # top1은 얕지만 top5 합산은 임계치 이상
        deep_units = [
            {"bid_price": 100.0, "bid_size": 1.0, "ask_price": 101.0, "ask_size": 1.0},
            {"bid_price": 100.0, "bid_size": 3.0, "ask_price": 101.0, "ask_size": 3.0},
            {"bid_price": 100.0, "bid_size": 3.0, "ask_price": 101.0, "ask_size": 3.0},
            {"bid_price": 100.0, "bid_size": 3.0, "ask_price": 101.0, "ask_size": 3.0},
            {"bid_price": 100.0, "bid_size": 3.0, "ask_price": 101.0, "ask_size": 3.0},
        ]
        with patch("trading_engine.pyupbit.get_orderbook", return_value={"orderbook_units": deep_units}):
            is_safe, _, details = engine.check_orderbook_safety("KRW-DOGE")
        self.assertTrue(is_safe)
        self.assertGreater(details["bid_depth_krw_5"], 1000)
        self.assertGreater(details["ask_depth_krw_5"], 1000)

        thin_units = [
            {"bid_price": 100.0, "bid_size": 1.0, "ask_price": 101.0, "ask_size": 1.0},
            {"bid_price": 100.0, "bid_size": 1.0, "ask_price": 101.0, "ask_size": 1.0},
            {"bid_price": 100.0, "bid_size": 1.0, "ask_price": 101.0, "ask_size": 1.0},
            {"bid_price": 100.0, "bid_size": 1.0, "ask_price": 101.0, "ask_size": 1.0},
            {"bid_price": 100.0, "bid_size": 1.0, "ask_price": 101.0, "ask_size": 1.0},
        ]
        with patch("trading_engine.pyupbit.get_orderbook", return_value={"orderbook_units": thin_units}):
            is_safe, reason, _ = engine.check_orderbook_safety("KRW-DOGE")
        self.assertFalse(is_safe)
        self.assertIn("LOW_LIQUIDITY", reason)
        self.assertTrue(logger.has_info("BUY_BLOCKED: LOW_LIQUIDITY"))

    def test_bear_regime_blocks_new_entries(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.update_global_regime = lambda force=False: ("BEAR", {})

        buy_signal, _, _, _, meta = engine.check_buy_signal("KRW-SOL")
        self.assertFalse(buy_signal)
        self.assertIn("GLOBAL_BEAR", meta.get("blocked_by", []))
        self.assertTrue(logger.has_info("BUY_BLOCKED: GLOBAL_BEAR"))

    def test_entry_time_block_has_expected_block_token(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.update_global_regime = lambda force=False: ("RANGE", {})
        engine._check_btc_trend_filter = lambda: (True, {"enabled": True})
        engine._is_entry_time_blocked = lambda: True

        buy_signal, _, _, _, meta = engine.check_buy_signal("KRW-ADA")
        self.assertFalse(buy_signal)
        self.assertIn("ENTRY_TIME_BLOCKED", meta.get("blocked_by", []))
        self.assertTrue(logger.has_info("BUY_BLOCKED: ENTRY_TIME_BLOCKED"))

    def test_unknown_limit_status_fails_closed_without_market_fallback(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.upbit = DummyUpbitUnknownStatus(cancel_returns=[None, None, None])

        with patch("trading_engine.pyupbit.get_current_price", return_value=100.0):
            with patch(
                "trading_engine.pyupbit.get_orderbook",
                return_value={"orderbook_units": [{"bid_price": 100.0, "ask_price": 101.0}]},
            ):
                result = engine.execute_buy("KRW-TEST", 10000)

        self.assertIsNone(result)
        self.assertEqual(len(engine.upbit.buy_limit_calls), 1)
        self.assertEqual(len(engine.upbit.buy_market_calls), 0)
        self.assertGreaterEqual(len(engine.upbit.cancel_calls), 1)
        self.assertTrue(logger.has_warning("LIMIT_ORDER_STATUS_UNKNOWN"))
        self.assertTrue(logger.has_info("LIMIT_ORDER_TIMEOUT_CANCEL_RESULT"))
        self.assertTrue(logger.has_error("CANCEL_FAILED_UNKNOWN_STATE"))
        self.assertTrue(logger.has_error("FALLBACK_ABORTED"))

    def test_unknown_limit_status_cancel_ok_still_fails_closed(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.upbit = DummyUpbitUnknownStatus(cancel_returns=[{"uuid": "buy-uuid", "state": "cancel"}])

        with patch("trading_engine.pyupbit.get_current_price", return_value=100.0):
            with patch(
                "trading_engine.pyupbit.get_orderbook",
                return_value={"orderbook_units": [{"bid_price": 100.0, "ask_price": 101.0}]},
            ):
                result = engine.execute_buy("KRW-TEST", 10000)

        self.assertIsNone(result)
        self.assertEqual(len(engine.upbit.buy_limit_calls), 1)
        self.assertEqual(len(engine.upbit.buy_market_calls), 0)
        self.assertTrue(logger.has_warning("LIMIT_ORDER_STATUS_UNKNOWN"))
        self.assertTrue(logger.has_info("LIMIT_ORDER_TIMEOUT_CANCEL_RESULT"))
        self.assertTrue(logger.has_warning("ABORT_FALLBACK_UNKNOWN_STATE"))
        self.assertFalse(logger.has_error("FALLBACK_ABORTED"))

    def test_select_strategy_supports_xrp_flow(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        state = {"ticker": "KRW-XRP"}

        self.assertEqual(engine.select_strategy(state, "BULL"), "XRP_FLOW")
        self.assertEqual(engine.select_strategy(state, "RANGE"), "XRP_FLOW")
        self.assertIsNone(engine.select_strategy(state, "BEAR"))

    def test_select_strategy_uses_symbol_strategy_map_config(self):
        config = make_config()
        config["strategy"]["symbol_strategy_map"] = {
            "SUI": {"strategy": "DOGE_MOMENTUM", "regimes": ["ANY"]},
        }
        logger = FakeLogger()
        engine = TradingEngine(config, logger, FakeStats())

        self.assertEqual(engine.select_strategy({"ticker": "KRW-SUI"}, "BULL"), "DOGE_MOMENTUM")
        self.assertEqual(engine.select_strategy({"ticker": "KRW-SUI"}, "BEAR"), "DOGE_MOMENTUM")
        self.assertIsNone(engine.select_strategy({"ticker": "KRW-XRP"}, "BULL"))

    def test_xrp_buy_signal_passes_with_valid_state(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.update_global_regime = lambda force=False: ("BULL", {})
        engine._check_btc_trend_filter = lambda: (True, {"enabled": True})
        engine._is_entry_time_blocked = lambda: False
        engine.analyze_symbol = lambda ticker: self._xrp_state()
        engine._size_by_risk = lambda ticker, entry_price, stop_price: {
            "equity_krw": 500000.0,
            "risk_krw": 2000.0,
            "risk_pct": 0.4,
            "qty_by_risk": 100.0,
            "risk_invest_krw": 10000.0,
            "weight_cap_krw": 200000.0,
            "weight_remaining_krw": 200000.0,
            "total_cap_remaining_krw": 200000.0,
            "recommended_invest_krw": 10000.0,
        }

        buy_signal, _, _, _, meta = engine.check_buy_signal("KRW-XRP")

        self.assertTrue(buy_signal)
        self.assertEqual(meta.get("strategy"), "XRP_FLOW")
        self.assertEqual(meta.get("blocked_by"), [])

    def test_xrp_buy_signal_blocks_on_rsi_band(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.update_global_regime = lambda force=False: ("BULL", {})
        engine._check_btc_trend_filter = lambda: (True, {"enabled": True})
        engine._is_entry_time_blocked = lambda: False
        engine.analyze_symbol = lambda ticker: self._xrp_state(rsi=80.0)
        engine._size_by_risk = lambda ticker, entry_price, stop_price: {
            "equity_krw": 500000.0,
            "risk_krw": 2000.0,
            "risk_pct": 0.4,
            "qty_by_risk": 100.0,
            "risk_invest_krw": 10000.0,
            "weight_cap_krw": 200000.0,
            "weight_remaining_krw": 200000.0,
            "total_cap_remaining_krw": 200000.0,
            "recommended_invest_krw": 10000.0,
        }

        buy_signal, _, _, _, meta = engine.check_buy_signal("KRW-XRP")

        self.assertFalse(buy_signal)
        self.assertIn("XRPRSI밴드미충족", meta.get("blocked_by", []))

    def test_xrp_sell_signal_time_stop(self):
        logger = FakeLogger()
        engine = TradingEngine(make_config(), logger, FakeStats())
        engine.analyze_symbol = lambda ticker: self._xrp_state()
        engine.get_current_price = lambda ticker: 101.0

        position = {
            "buy_price": 100.0,
            "highest_price": 101.0,
            "amount": 10.0,
            "timestamp": datetime.now() - timedelta(minutes=120),
            "buy_meta": {
                "strategy": "XRP_FLOW",
                "stop_price": 99.0,
                "target_r": 1.2,
                "time_stop_candles": 2,
            },
        }

        should_sell, reason, _, _ = engine.check_sell_signal("KRW-XRP", position)

        self.assertTrue(should_sell)
        self.assertIn("XRP 시간청산", reason)


if __name__ == "__main__":
    unittest.main()
