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
from trading_engine import TradingEngine
from telegram_notifier import TelegramNotifier
from version import BOT_NAME, BOT_DISPLAY_NAME, BOT_VERSION


class TradingBot:
    def __init__(self, config_path='config.json'):
        # 설정 로드
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        
        # 모듈 초기화
        self.logger = TradingLogger(self.config)
        self.stats = TradingStats()
        self.engine = TradingEngine(self.config, self.logger, self.stats)
        self.telegram = TelegramNotifier(self.config)
        self.bot_name = BOT_NAME
        self.bot_display_name = BOT_DISPLAY_NAME
        self.bot_version = BOT_VERSION
        
        # 상태 변수
        self.is_running = False
        self.trading_thread = None
        self.target_coins = []
        self.is_trading_paused = False
        self.cooldown_until = None  # 쿨다운 종료 시간
        
        # 중복 매수 방지
        self.buying_in_progress = set()  # 현재 매수 중인 코인들
        self.buy_lock = threading.Lock()  # 매수 Lock
        
        # 설정값: 최대 동시 포지션은 strategy.max_positions 단일 기준
        trading_cfg = self.config.get('trading', {}) or {}
        strategy_cfg = self.config.get('strategy', {}) or {}
        engine_max_positions = getattr(self.engine, "max_positions", None)
        try:
            configured_max_positions = int(
                strategy_cfg.get('max_positions', engine_max_positions if engine_max_positions is not None else 3)
            )
        except Exception:
            configured_max_positions = int(engine_max_positions or 3)

        self.max_coins = max(1, configured_max_positions)
        self.max_total_investment = trading_cfg.get('max_total_investment', 300000)
        _auto = trading_cfg.get('auto_start_on_launch', True)
        self.auto_start_on_launch = True if _auto is None else bool(_auto)
        self.check_interval = int(trading_cfg.get('check_interval_seconds', 10))
        self.last_buy_attempt_candle = {}  # ticker -> candle_ts
        self._last_buy_block_signature = {}  # ticker -> dedupe signature
        try:
            hb = int(trading_cfg.get('analysis_heartbeat_minutes', 10))
            self.analysis_heartbeat_minutes = max(1, hb)
        except Exception:
            self.analysis_heartbeat_minutes = 10
        self._last_analysis_heartbeat_at = None
        
        # 손절 후 동일 종목 재진입 쿨다운(과매매/휘둘림 방지)
        try:
            self.reentry_cooldown_after_stoploss_minutes = int(
                trading_cfg.get('reentry_cooldown_after_stoploss_minutes', 0) or 0
            )
        except Exception:
            self.reentry_cooldown_after_stoploss_minutes = 0
        self.reentry_cooldowns = {}  # ticker -> datetime(until)
        
        # 일일 손실 제한
        self.daily_loss_limit = trading_cfg.get('daily_loss_limit_percent', -5.0)
        self.cooldown_minutes = trading_cfg.get('cooldown_after_loss_minutes', 30)
        
        # 거래 시간 필터
        trading_hours_cfg = trading_cfg.get('trading_hours', {}) or {}
        self.trading_hours_enabled = trading_hours_cfg.get('enabled', False)
        self.trading_sessions = trading_hours_cfg.get('sessions', [])
        
        # 미기록 잔고 처리 설정
        untracked_cfg = trading_cfg.get('untracked_balance', {}) or {}
        self.untracked_action = str(untracked_cfg.get('action', 'ignore')).lower()
        self.untracked_cleanup_max_krw = untracked_cfg.get('cleanup_max_krw', 20000)
        
        # 보호 종목은 excluded_coins 단일 목록으로 통일
        excluded = set(self.config['coin_selection'].get('excluded_coins', []))
        self.protected_coins = {self._to_symbol(c) for c in excluded if c}
    
    def start(self):
        """트레이딩 시작"""
        
        if self.is_running:
            print("⚠️  이미 실행 중입니다.")
            return
        
        self.logger.info("="*80)
        self.logger.info(f"🚀 {self.bot_display_name} 시작 (v{self.bot_version})")
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

        # 포지션 복구 시도
        saved_positions = self.stats.load_positions()
        if saved_positions:
            self.logger.info(f"💾 저장된 포지션 발견: {len(saved_positions)}개")
            
            # 계정 잔고와 대조 (Reconcile)
            reconciled_positions = {}
            
            for coin, saved_pos in saved_positions.items():
                actual_balance = self.engine.upbit.get_balance(coin)
                saved_amount = saved_pos['amount']
                
                # 실제 잔고가 없으면 포지션 제거
                if actual_balance <= 0 and saved_amount > 0:
                    self.logger.warning(f"⚠️  {coin} 포지션은 있으나 실제 잔고 없음 → 스냅샷에서 제거")
                    continue  # 복구하지 않음
                
                # 실제 잔고가 있으면 차이 확인
                if actual_balance > 0:
                    diff_pct = abs(actual_balance - saved_amount) / saved_amount * 100 if saved_amount > 0 else 100
                    
                    if diff_pct > 5.0:  # 5% 이상 차이
                        self.logger.warning(f"⚠️  {coin} 수량 불일치: 저장 {saved_amount:.8f} vs 실제 {actual_balance:.8f} ({diff_pct:.1f}%)")
                        # 실제 잔고로 업데이트
                        saved_pos['amount'] = actual_balance
                        self.logger.info(f"   → 실제 잔고로 업데이트: {actual_balance:.8f}")
                    
                    reconciled_positions[coin] = saved_pos
            
            if reconciled_positions:
                self.stats.positions = reconciled_positions
                self.logger.info(f"✅ 포지션 복구 완료: {len(reconciled_positions)}개")
                
                # 스냅샷 업데이트 (정리된 포지션으로)
                self.stats.save_positions()
            else:
                self.logger.info("📝 복구할 포지션이 없습니다")
        
        # 스냅샷에 없는 실제 잔고 처리
        self._sync_untracked_balances()

        # 시작 총자산(현금+보유 포지션 평가액) 기준선 설정 (재기동 시 수익률 왜곡 방지)
        initial_total_value = self._estimate_total_value(initial_balance)
        positions_value = max(0.0, float(initial_total_value) - float(initial_balance))
        self.logger.info(
            f"📌 시작 총자산 기준선: {initial_total_value:,.0f}원 "
            f"(현금 {float(initial_balance):,.0f}원 + 포지션 {positions_value:,.0f}원)"
        )
        self.stats.start(initial_balance, initial_total_value=initial_total_value)
        
        # 초기 레짐 계산
        try:
            regime, _ = self.engine.update_global_regime(force=True)
            self.logger.info(f"🌐 초기 글로벌 레짐: {regime}")
        except Exception as e:
            self.logger.warning(f"⚠️ 초기 레짐 계산 실패: {e}")

        # 고정 종목 목록 적용
        self._apply_fixed_target_coins(reason="startup")
        if not self.target_coins:
            self.logger.warning("⚠️ 고정 거래 종목이 비어 있습니다. config의 fixed_tickers를 확인하세요.")

        # 분석 로그: 세션 시작/초기 선정
        self.logger.log_decision(
            "START",
            {
                "version": self.bot_version,
                "initial_cash_krw": float(initial_balance),
                "initial_total_value_krw": float(initial_total_value),
                "initial_positions_value_krw": float(positions_value),
                "recovered_positions": list(self.stats.positions.keys()),
                "selected_coins": list(self.target_coins),
                "global_regime": getattr(self.engine, "global_regime", "RANGE"),
                "protected_coins": sorted(list(self.protected_coins)),
                "config": {
                    "max_positions": int(self.max_coins),
                    "check_interval_seconds": float(self.check_interval),
                    "analysis_heartbeat_minutes": int(self.analysis_heartbeat_minutes),
                    "max_total_investment_krw": float(self.max_total_investment),
                    "strategy": {
                        "mode": self.config.get("strategy", {}).get("mode", "regime"),
                        "entry_interval": self.config.get("strategy", {}).get("entry_interval", "minute5"),
                        "signal_candle_minutes": int(self.config.get("strategy", {}).get("signal_candle_minutes", 20) or 20),
                        "regime_reference": self.config.get("strategy", {}).get("regime_reference", "KRW-BTC"),
                        "regime_check_minutes": int(self.config.get("strategy", {}).get("regime_check_minutes", 20) or 20),
                        "regime_confirm_count": int(self.config.get("strategy", {}).get("regime_confirm_count", 3) or 3),
                        "regime_min_hold_minutes": int(self.config.get("strategy", {}).get("regime_min_hold_minutes", 0) or 0),
                        "universe": list(self.config.get("strategy", {}).get("universe", []) or []),
                        "entry_time_filter": dict(self.config.get("strategy", {}).get("entry_time_filter", {}) or {}),
                        "btc_filter": dict(self.config.get("strategy", {}).get("btc_filter", {}) or {}),
                        "volatility_tr_atr_max": float(self.config.get("strategy", {}).get("volatility_tr_atr_max", 3.0) or 3.0),
                        "risk_per_symbol_pct": dict(self.config.get("strategy", {}).get("risk_per_symbol_pct", {}) or {}),
                        "fixed_tickers": list(self.config.get("coin_selection", {}).get("fixed_tickers", []) or []),
                        "excluded_coins": list(self.config.get("coin_selection", {}).get("excluded_coins", []) or []),
                    },
                    "risk_management": {
                        "risk_per_trade_pct": float(self.config.get("risk_management", {}).get("risk_per_trade_pct", 1.0) or 1.0),
                        "risk_per_symbol_pct": dict(self.config.get("risk_management", {}).get("risk_per_symbol_pct", {}) or {}),
                        "time_stop_candles": int(self.config.get("risk_management", {}).get("time_stop_candles", 10) or 10),
                        "min_hold_minutes": int(self.config.get("risk_management", {}).get("min_hold_minutes", 20) or 20),
                        "max_hold_minutes": int(self.config.get("risk_management", {}).get("max_hold_minutes", 360) or 360),
                        "trailing_stop_pct": float(self.config.get("risk_management", {}).get("trailing_stop_pct", 1.0) or 1.0),
                        "trailing_activation_pct": float(self.config.get("risk_management", {}).get("trailing_activation_pct", 2.0) or 2.0),
                    },
                    "fee_pct": self.config.get("trading", {}).get("fee_pct", None),
                },
            },
        )
        
        # 거래 시작
        self.is_running = True
        self.trading_thread = threading.Thread(target=self._trading_loop, daemon=True)
        self.trading_thread.start()
        
        # 시작 시점 시장 상황 스냅샷
        market_snapshot = self._get_market_snapshot(probe=True)

        # 매수 조건 출력
        self._print_trading_conditions(market_snapshot=market_snapshot)
        
        # 텔레그램 알림
        self.telegram.notify_start(
            bot_name=self.bot_name,
            bot_version=self.bot_version,
            display_name=self.bot_display_name,
            selected_coins=self.target_coins,
            market_summary_lines=self._format_market_snapshot_lines(market_snapshot),
        )
        
        # 텔레그램 명령어 수신 시작
        if self.telegram.enable_commands:
            self.telegram.start_listening(self._handle_telegram_command)
            self.logger.info("📱 텔레그램 명령어 수신 시작")
        
        if self.target_coins:
            print("✅ 트레이딩 시작됨")
        else:
            print("✅ 트레이딩 대기 시작됨 (고정 종목 설정 확인 필요)")
    
    def _print_trading_conditions(self, market_snapshot=None):
        """현재 매수 조건 출력"""

        print("\n" + "="*80)
        print("📋 현재 매수 조건")
        print("="*80)

        strategy_cfg = self.config.get("strategy", {}) or {}
        risk_cfg = self.config.get("risk_management", {}) or {}

        mode = strategy_cfg.get("mode", "regime")
        entry_interval = strategy_cfg.get("entry_interval", "minute20")
        signal_candle_minutes = int(strategy_cfg.get("signal_candle_minutes", 20) or 20)
        regime_check_minutes = strategy_cfg.get("regime_check_minutes", 20)
        regime_confirm_count = strategy_cfg.get("regime_confirm_count", 3)
        regime_min_hold_minutes = strategy_cfg.get("regime_min_hold_minutes", 0)
        max_positions = strategy_cfg.get("max_positions", self.max_coins)
        universe = strategy_cfg.get("universe", ["SOL", "DOGE", "ADA"])
        no_entry = strategy_cfg.get("entry_time_filter", {}) or {}
        btc_filter = strategy_cfg.get("btc_filter", {}) or {}
        risk_per_symbol = strategy_cfg.get("risk_per_symbol_pct", {}) or risk_cfg.get("risk_per_symbol_pct", {}) or {}

        if market_snapshot is None:
            market_snapshot = self._get_market_snapshot(probe=True)
        market_lines = self._format_market_snapshot_lines(market_snapshot)

        print("\n🎯 전략 모드")
        print(f"  {mode} (레짐이 전략을 결정)")
        print(f"  현재 레짐: {getattr(self.engine, 'global_regime', 'RANGE')}")
        print(f"  레짐 갱신 주기: {regime_check_minutes}분 | 기준 봉: {signal_candle_minutes}분")
        print(f"  전환 확정: {regime_confirm_count}회 연속 (최소 유지 {regime_min_hold_minutes}분)")

        print("\n🌐 현재 시장 상황")
        for line in market_lines:
            print(f"  {line}")

        print("\n📌 레짐별 전략")
        print("  SOL: BULL 레짐에서 48봉 돌파+리테스트")
        print("  DOGE: 거래량 스파이크 + RSI 모멘텀 + EMA20 풀백")
        print("  ADA: RANGE 레짐에서 RSI 과매도 + 96봉 하단 15%")

        print("\n📌 공통 진입 게이트")
        print(f"  1) 시간 필터: {no_entry.get('start_hour', 2):02d}:00~{no_entry.get('end_hour', 6):02d}:00 신규 진입 차단")
        print(f"  2) BTC 필터: {btc_filter.get('ticker', 'KRW-BTC')} 종가 > EMA{btc_filter.get('ema_period', 50)}")
        print(f"  3) 변동성 필터: TR/ATR <= {strategy_cfg.get('volatility_tr_atr_max', 3.0)}")
        print("  4) 동시 포지션 최대 2개")

        time_stop_candles = risk_cfg.get("time_stop_candles", 10)
        risk_per_trade_pct = risk_cfg.get("risk_per_trade_pct", 1.0)
        min_hold = risk_cfg.get("min_hold_minutes", 20)
        max_hold = risk_cfg.get("max_hold_minutes", 360)

        print("\n📌 손절/청산 핵심")
        print("  1) SOL: 손절 0.5*ATR, 1.2R 30% 익절, 2.2R 이후 트레일링")
        print("  2) DOGE: 손절 -0.8%, 6캔들 시간청산")
        print("  3) ADA: 손절 -0.9%, 96봉 상단 85% 목표청산")
        print(f"  4) 공통 최대보유: {max_hold}분 (기본 시간손절 {time_stop_candles}캔들)")

        print("\n💰 자금 운용")
        print(f"  최대 투자 한도: {self.max_total_investment:,.0f}원")
        print(f"  최대 동시 포지션: {max_positions}개")
        print(f"  기본 유니버스: {', '.join(universe)}")
        print(f"  종목별 리스크(%): {risk_per_symbol if risk_per_symbol else risk_per_trade_pct}")
        print("  사이징: 계좌 리스크/손절거리 기반 + 종목별 비중 상한")

        print("\n🛡️ 안전 장치")
        print(f"  최대 스프레드: {self.config['trading'].get('max_spread_percent', 0.5)}%")
        print(f"  최소 호가잔량: {self.config['trading'].get('min_orderbook_depth_krw', 5000000):,}원")

        print("\n⏰ 거래 시간")
        if self.config["trading"]["trading_hours"].get("enabled", False):
            sessions = self.config["trading"]["trading_hours"]["sessions"]
            print("  ✅ 시간 필터 사용:")
            for session in sessions:
                print(f"     {session['start']:02d}:00 ~ {session['end']:02d}:00")
        else:
            print("  ❌ 24시간 거래")

        if self.protected_coins:
            print(f"\n🛡️ 보호 종목(미개입): {', '.join(sorted(self.protected_coins))}")

        print("="*80)
    
    def _to_symbol(self, ticker_or_symbol):
        """티커/심볼을 심볼(예: BTC)로 표준화"""
        if not ticker_or_symbol:
            return ""
        
        value = str(ticker_or_symbol).upper()
        if '-' in value:
            return value.split('-')[-1]
        return value

    def _to_ticker(self, ticker_or_symbol):
        """티커/심볼을 KRW-XXX 형태로 표준화"""
        symbol = self._to_symbol(ticker_or_symbol)
        return f"KRW-{symbol}" if symbol else ""

    def _regime_label(self, regime):
        value = str(regime or "").upper()
        labels = {
            "BULL": "상승장(BULL)",
            "BEAR": "하락장(BEAR)",
            "RANGE": "횡보장(RANGE)",
        }
        return labels.get(value, value or "알 수 없음")

    def _get_market_snapshot(self, probe=True):
        """현재 시장 상황(레짐/기준지표) 스냅샷."""
        snapshot = {
            "global_regime": str(getattr(self.engine, "global_regime", "RANGE") or "RANGE"),
            "candidate": "",
            "reference_ticker": str(getattr(self.engine, "regime_reference_ticker", "KRW-BTC") or "KRW-BTC"),
            "close": None,
            "ema50": None,
            "ema200": None,
            "candle_ts": "",
            "error": "",
        }

        if not probe:
            return snapshot

        try:
            candidate, detect_meta = self.engine.detect_global_regime()
            snapshot["candidate"] = str(candidate or "")
            if isinstance(detect_meta, dict):
                snapshot["reference_ticker"] = str(
                    detect_meta.get("reference_ticker") or snapshot["reference_ticker"]
                )
                snapshot["close"] = detect_meta.get("close")
                snapshot["ema50"] = detect_meta.get("ema50")
                snapshot["ema200"] = detect_meta.get("ema200")
                snapshot["candle_ts"] = str(detect_meta.get("candle_ts") or "")
                if not snapshot["candidate"]:
                    snapshot["candidate"] = str(detect_meta.get("candidate") or "")
        except Exception as e:
            snapshot["error"] = str(e).replace("<", "(").replace(">", ")")

        return snapshot

    def _format_market_snapshot_lines(self, snapshot):
        """시장 상황 텍스트 라인 목록 생성."""
        data = snapshot if isinstance(snapshot, dict) else {}
        current = str(data.get("global_regime", "RANGE") or "RANGE").upper()
        candidate = str(data.get("candidate", "") or "").upper()
        ref = str(data.get("reference_ticker", "KRW-BTC") or "KRW-BTC")

        lines = [f"현재 레짐: {self._regime_label(current)}"]
        if candidate and candidate != current:
            lines.append(f"탐지 후보: {self._regime_label(candidate)}")
        lines.append(f"기준 자산: {ref}")

        close = data.get("close")
        ema50 = data.get("ema50")
        ema200 = data.get("ema200")
        try:
            if close is not None and ema50 is not None and ema200 is not None:
                lines.append(
                    f"종가/EMA50/EMA200: {float(close):,.0f} / {float(ema50):,.0f} / {float(ema200):,.0f}"
                )
        except Exception:
            pass

        candle_ts = str(data.get("candle_ts", "") or "")
        if candle_ts:
            lines.append(f"기준 캔들: {candle_ts}")

        error = str(data.get("error", "") or "")
        if error:
            lines.append(f"조회 상태: {error}")

        return lines

    def _resolve_fixed_target_coins(self):
        """고정 거래 종목 목록 계산(excluded_coins 제외)."""
        coin_cfg = self.config.get("coin_selection", {}) or {}
        strategy_cfg = self.config.get("strategy", {}) or {}

        raw = coin_cfg.get("fixed_tickers", []) or strategy_cfg.get("universe", []) or ["SOL", "DOGE", "ADA"]
        excluded = {self._to_symbol(c) for c in coin_cfg.get("excluded_coins", []) if c}

        out = []
        for item in raw:
            ticker = self._to_ticker(item)
            symbol = self._to_symbol(ticker)
            if not ticker:
                continue
            if symbol in excluded:
                continue
            if ticker not in out:
                out.append(ticker)

        if self.max_coins > 0:
            out = out[: int(self.max_coins)]

        return out

    def _apply_fixed_target_coins(self, reason="manual"):
        """고정 종목 목록을 target_coins에 재적용."""
        old = list(self.target_coins or [])
        new = self._resolve_fixed_target_coins()

        self.target_coins = list(new)

        added = [c for c in new if c not in old]
        removed = [c for c in old if c not in new]

        self.logger.log_decision(
            "COIN_REFRESH",
            {
                "reason": reason,
                "mode": "fixed_tickers_only",
                "ok": bool(new),
                "selected": list(new),
                "added": list(added),
                "removed": list(removed),
            },
        )
        return list(new), list(added), list(removed)
    
    def _is_protected_coin(self, ticker_or_symbol):
        """예외 종목(수동 관리) 여부 확인"""
        symbol = self._to_symbol(ticker_or_symbol)
        return symbol in self.protected_coins
    
    def _is_reentry_cooldown_active(self, ticker):
        """손절 직후 같은 종목 재진입(재매수) 방지"""
        until = self.reentry_cooldowns.get(ticker)
        if not until:
            return False
        
        if datetime.now() >= until:
            self.reentry_cooldowns.pop(ticker, None)
            return False
        
        return True
    
    def _set_reentry_cooldown(self, ticker, minutes, reason=""):
        """손절 이후 동일 종목 재진입 쿨다운 설정"""
        if minutes <= 0:
            return
        
        until = datetime.now() + timedelta(minutes=minutes)
        self.reentry_cooldowns[ticker] = until
        self.logger.info(
            f"⏳ 재진입 쿨다운 설정: {ticker} | {minutes}분 | 사유: {reason} | "
            f"해제: {until.strftime('%H:%M:%S')}"
        )
    
    def _sync_untracked_balances(self):
        """스냅샷에 없는 실제 잔고를 설정에 따라 편입/정리"""
        try:
            balances = self.engine.upbit.get_balances()
            if not balances:
                return
            
            for bal in balances:
                currency = bal.get('currency')
                if not currency or currency == "KRW":
                    continue
                
                amount = float(bal.get('balance', 0) or 0)
                if amount <= 0:
                    continue
                
                ticker = f"KRW-{currency}"
                
                # 이미 포지션이면 스킵
                if ticker in self.stats.positions:
                    continue
                
                # 보호 종목이면 미개입
                if self._is_protected_coin(currency):
                    self.logger.info(f"🛡️ 보호 종목 잔고 감지(미개입): {ticker} {amount:.8f}")
                    continue
                
                self._handle_untracked_balance(ticker, amount, is_startup=True)
        
        except Exception as e:
            self.logger.log_error("미기록 잔고 동기화 오류", e)
    
    def _handle_untracked_balance(self, ticker, actual_balance, is_startup=False):
        """
        스냅샷에 없는 실제 잔고 처리.
        Returns:
            bool: 처리/스킵 완료 여부 (True면 추가 매수 검토 중단)
        """
        # 보호 종목은 무조건 미개입
        if self._is_protected_coin(ticker):
            if is_startup:
                self.logger.info(f"🛡️ 보호 종목이므로 미기록 잔고 처리 제외: {ticker}")
            return True
        
        action = self.untracked_action
        
        # 1) 편입 모드: 봇 포지션으로 편입
        if action == "attach":
            if ticker not in self.stats.positions:
                coin = ticker.split('-')[1]
                buy_price = self.engine.upbit.get_avg_buy_price(coin)
                
                if not buy_price or buy_price <= 0:
                    market_price = self.engine.get_current_price(ticker)
                    buy_price = market_price if market_price and market_price > 0 else 0
                
                if buy_price and buy_price > 0:
                    self.stats.add_position(ticker, buy_price, actual_balance, "external-balance")
                    self.logger.warning(
                        f"📥 미기록 잔고 편입: {ticker} | 수량 {actual_balance:.8f} | 기준가 {buy_price:,.0f}"
                    )
                else:
                    self.logger.warning(f"⚠️ {ticker} 미기록 잔고 편입 실패: 기준가 조회 불가")
            return True
        
        # 2) 소액 정리 모드: 지정 금액 이하만 자동 정리
        if action == "cleanup_small":
            current_price = self.engine.get_current_price(ticker)
            if not current_price:
                self.logger.warning(f"⚠️ {ticker} 현재가 조회 실패로 소액 정리 보류")
                return True
            
            est_krw = actual_balance * current_price
            min_trade = self.config['trading']['min_trade_amount']
            
            if est_krw < min_trade:
                self.logger.info(f"💤 {ticker} 잔고 소액({est_krw:,.0f}원)으로 정리 불가, 보류")
                return True
            
            if est_krw <= self.untracked_cleanup_max_krw:
                temp_position = {
                    'buy_price': current_price,
                    'amount': actual_balance,
                    'timestamp': datetime.now(),
                    'highest_price': current_price
                }
                sell_result = self.engine.execute_sell(ticker, temp_position, 1.0)
                if sell_result:
                    self.logger.warning(f"🧹 미기록 소액 잔고 정리 완료: {ticker} | 약 {est_krw:,.0f}원")
                else:
                    self.logger.warning(f"⚠️ {ticker} 미기록 소액 잔고 정리 실패")
                return True
            
            self.logger.info(
                f"📌 {ticker} 미기록 잔고 유지: {est_krw:,.0f}원 > 정리한도 {self.untracked_cleanup_max_krw:,.0f}원"
            )
            return True
        
        # 3) 기본 모드(ignore): 기존 동작 유지
        return False
    
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
                    # 수수료 누적(가능하면 실제, 없으면 추정)
                    self.stats.add_fee(sell_result.get('fee', 0))

                    remaining_amount = sell_result.get('remaining_amount')
                    if remaining_amount is None:
                        remaining_amount = self.engine.get_tradable_balance(coin)
                    
                    min_trade = self.config['trading']['min_trade_amount']
                    ref_price = self.engine.get_current_price(coin) or sell_result['price']
                    remaining_value = remaining_amount * ref_price if ref_price else 0
                    
                    # 전량 청산 시에도 잔량이 주문 가능하면 포지션 유지
                    if remaining_amount > 0 and remaining_value >= min_trade:
                        position['amount'] = remaining_amount
                        self.stats.save_positions()
                        self.logger.warning(
                            f"⚠️ 정지 청산 후 잔량 남음: {coin} | "
                            f"{remaining_amount:.8f} ({remaining_value:,.0f}원) | 포지션 유지"
                        )
                        continue
                    
                    sold_cost = position['buy_price'] * sell_result['amount']
                    profit_krw = sell_result['total_krw'] - sold_cost
                    self.stats.remove_position(
                        coin,
                        sell_result['price'],
                        profit_krw,
                        "정지시 청산",
                        sell_fee_krw=sell_result.get('fee', 0),
                        sell_meta={"note": "정지시 청산"},
                    )
        
        # 최종 잔고
        final_balance = self.engine.get_balance("KRW")
        final_total_value = self._estimate_total_value(final_balance)
        self.stats.update_balance(final_balance, current_total_value=final_total_value)
        
        # 통계 저장
        self.logger.log_daily_stats(self.stats.get_current_status())
        
        # 텔레그램 알림
        final_status = self.stats.get_current_status()
        total_profit = final_status['total_value'] - self.stats.initial_balance
        self.telegram.notify_stop(final_status['total_value'], total_profit)
        
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
            
            # /weekly - 주간 통계(최근 7일)
            elif cmd == '/weekly' or cmd == '/주간':
                self._telegram_weekly()
            
            # /positions - 포지션 현황
            elif cmd == '/positions' or cmd == '/포지션':
                self._telegram_positions()
            
            # /balance - 잔고
            elif cmd == '/balance' or cmd == '/잔고':
                self._telegram_balance()
            
            # /pause - 일시 정지
            elif cmd == '/pause' or cmd == '/정지':
                self._telegram_pause()
            
            # /resume - 재개
            elif cmd == '/resume' or cmd == '/재개':
                self._telegram_resume()
            
            # /help - 도움말
            elif cmd == '/help' or cmd == '/도움말':
                self._telegram_help()
            
            # /version - 버전 정보
            elif cmd == '/version' or cmd == '/버전':
                self._telegram_version()
            
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
        market_snapshot = self._get_market_snapshot(probe=True)
        market_lines = self._format_market_snapshot_lines(market_snapshot)
        market_block = "\n".join(market_lines)
        
        # 사용 가능 금액 계산
        invested = sum(pos['buy_price'] * pos['amount'] for pos in self.stats.positions.values())
        cap_remaining = max(0.0, self.max_total_investment - invested)
        available = max(0.0, min(cap_remaining, status['current_balance']))

        # 수수료/예상 청산 수수료(보유 포지션 기준)
        fee_rate = getattr(self.engine, "FEE", 0.0005)
        positions_value = max(0.0, float(status.get('total_value', 0) or 0) - float(status.get('current_balance', 0) or 0))
        est_exit_fee = positions_value * fee_rate
        
        state = "▶️ 실행 중" if self.is_running else "⏸️ 정지"
        if self.is_trading_paused:
            state += " (시간외)"
        if self.cooldown_until:
            state += " (쿨다운)"
        
        message = f"""📊 <b>현재 상태</b>

🔄 상태: {state}

🌐 <b>시장 상황</b>
{market_block}

💰 <b>자금 현황</b>
초기: {status['initial_balance']:,.0f}원
현재: {status['current_balance']:,.0f}원
투자중: {invested:,.0f}원
사용가능: {available:,.0f}원

📈 <b>수익</b>
총 평가액: {status['total_value']:,.0f}원
총 수익률: {status['total_return']:+.2f}%

💸 <b>수수료(추정)</b>
누적(세션): {status.get('total_fees_krw', 0):,.0f}원
예상 청산: {est_exit_fee:,.0f}원

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

        fee_rate = getattr(self.engine, "FEE", 0.0005)
        buy_fee_sum = 0.0
        sell_fee_sum = 0.0
        total_profit = 0.0
        total_profit_after_fees = 0.0
        turnover_krw = 0.0

        def _paf(tr):
            paf = tr.get('profit_after_fees_krw', None)
            if paf is not None:
                return float(paf or 0)
            try:
                bp = float(tr.get('buy_price', 0) or 0)
                amt = float(tr.get('amount', 0) or 0)
                bf = float(tr.get('buy_fee_krw', 0) or 0)
                if bf <= 0:
                    bf = bp * amt * fee_rate
                return float(tr.get('profit_krw', 0) or 0) - bf
            except Exception:
                return float(tr.get('profit_krw', 0) or 0)

        for t in today_trades:
            try:
                total_profit += float(t.get('profit_krw', 0) or 0)
                total_profit_after_fees += _paf(t)

                buy_price = float(t.get('buy_price', 0) or 0)
                sell_price = float(t.get('sell_price', 0) or 0)
                amount = float(t.get('amount', 0) or 0)
                turnover_krw += (buy_price * amount) + (sell_price * amount)

                buy_fee = t.get('buy_fee_krw', None)
                sell_fee = t.get('sell_fee_krw', None)
                if buy_fee is None:
                    buy_fee = buy_price * amount * fee_rate
                if sell_fee is None:
                    sell_fee = sell_price * amount * fee_rate
                buy_fee_sum += float(buy_fee or 0)
                sell_fee_sum += float(sell_fee or 0)
            except Exception:
                continue

        wins = [t for t in today_trades if _paf(t) > 0]
        losses = [t for t in today_trades if _paf(t) <= 0]
        total_fee_sum = buy_fee_sum + sell_fee_sum
        fee_turnover_str = f"{(total_fee_sum/turnover_krw*100):.3f}%" if turnover_krw > 0 else "N/A"
        strategy_profit = {}
        strategy_count = {}
        strategy_wins = {}
        for t in today_trades:
            buy_meta = t.get("buy_meta", {}) if isinstance(t.get("buy_meta"), dict) else {}
            strategy = str(t.get("strategy") or buy_meta.get("strategy") or "UNKNOWN")
            strategy_profit[strategy] = strategy_profit.get(strategy, 0.0) + _paf(t)
            strategy_count[strategy] = strategy_count.get(strategy, 0) + 1
            if _paf(t) > 0:
                strategy_wins[strategy] = strategy_wins.get(strategy, 0) + 1
        
        message = f"""📅 <b>일일 통계</b>

날짜: {today.strftime('%Y-%m-%d')}

📊 거래: {len(today_trades)}회
✅ 승: {len(wins)}회
❌ 패: {len(losses)}회
📈 승률: {len(wins)/len(today_trades)*100:.1f}%

💰 총 손익: {total_profit:+,.0f}원
💰 총 손익(수수료 반영): {total_profit_after_fees:+,.0f}원
💸 수수료(기간): {total_fee_sum:,.0f}원 (매수 {buy_fee_sum:,.0f} + 매도 {sell_fee_sum:,.0f})
거래대금(왕복): {turnover_krw:,.0f}원
수수료/거래대금: {fee_turnover_str}
💸 누적 수수료(세션): {self.stats.get_total_fees_krw():,.0f}원
"""
        
        if wins:
            best = max(wins, key=_paf)
            message += f"\n🏆 최고: {best['coin'].replace('KRW-', '')} {_paf(best):+,.0f}원"
        
        if losses:
            worst = min(losses, key=_paf)
            message += f"\n📉 최악: {worst['coin'].replace('KRW-', '')} {_paf(worst):+,.0f}원"

        if strategy_count:
            message += "\n\n🧠 <b>전략별 성과</b>"
            ranked = sorted(strategy_profit.items(), key=lambda kv: kv[1], reverse=True)
            for strategy, pnl in ranked:
                cnt = strategy_count.get(strategy, 0)
                win = strategy_wins.get(strategy, 0)
                wr = (win / cnt * 100) if cnt > 0 else 0
                message += f"\n{strategy}: {pnl:+,.0f}원 ({cnt}회, 승률 {wr:.1f}%)"
        
        self.telegram.send_message(message)

    def _telegram_weekly(self):
        """텔레그램: 주간 리포트 (최근 7일)"""
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=6)
        
        # 파일 + 메모리 통합 (중복 제거: timestamp 기준)
        all_trades_dict = {}
        
        for i in range(7):
            d = start_date + timedelta(days=i)
            # load_daily_trades는 datetime 또는 YYYYMMDD 문자열을 받음
            file_trades = self.stats.load_daily_trades(datetime.combine(d, datetime.min.time()))
            for t in file_trades:
                all_trades_dict[t['timestamp'].isoformat()] = t
        
        memory_trades = [
            t for t in self.stats.trades
            if start_date <= t['timestamp'].date() <= end_date
        ]
        for t in memory_trades:
            all_trades_dict[t['timestamp'].isoformat()] = t
        
        week_trades = list(all_trades_dict.values())
        
        if not week_trades:
            self.telegram.send_message(
                f"📆 최근 7일 거래 내역이 없습니다.\n\n"
                f"기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}"
            )
            return

        fee_rate = getattr(self.engine, "FEE", 0.0005)

        def _paf(tr):
            paf = tr.get('profit_after_fees_krw', None)
            if paf is not None:
                return float(paf or 0)
            try:
                bp = float(tr.get('buy_price', 0) or 0)
                amt = float(tr.get('amount', 0) or 0)
                bf = float(tr.get('buy_fee_krw', 0) or 0)
                if bf <= 0:
                    bf = bp * amt * fee_rate
                return float(tr.get('profit_krw', 0) or 0) - bf
            except Exception:
                return float(tr.get('profit_krw', 0) or 0)

        buy_fee_sum = 0.0
        sell_fee_sum = 0.0
        total_profit = 0.0
        total_profit_after_fees = 0.0
        turnover_krw = 0.0

        for t in week_trades:
            try:
                total_profit += float(t.get('profit_krw', 0) or 0)
                total_profit_after_fees += _paf(t)

                buy_price = float(t.get('buy_price', 0) or 0)
                sell_price = float(t.get('sell_price', 0) or 0)
                amount = float(t.get('amount', 0) or 0)
                turnover_krw += (buy_price * amount) + (sell_price * amount)

                buy_fee = t.get('buy_fee_krw', None)
                sell_fee = t.get('sell_fee_krw', None)
                if buy_fee is None:
                    buy_fee = buy_price * amount * fee_rate
                if sell_fee is None:
                    sell_fee = sell_price * amount * fee_rate
                buy_fee_sum += float(buy_fee or 0)
                sell_fee_sum += float(sell_fee or 0)
            except Exception:
                continue

        wins = [t for t in week_trades if _paf(t) > 0]
        losses = [t for t in week_trades if _paf(t) <= 0]
        win_rate = (len(wins) / len(week_trades) * 100) if week_trades else 0

        total_fee_sum = buy_fee_sum + sell_fee_sum
        fee_turnover_str = f"{(total_fee_sum/turnover_krw*100):.3f}%" if turnover_krw > 0 else "N/A"
        
        best = max(week_trades, key=_paf)
        worst = min(week_trades, key=_paf)
        
        # 일자별 손익/횟수
        daily_profit = {}
        daily_count = {}
        for i in range(7):
            d = start_date + timedelta(days=i)
            daily_profit[d] = 0
            daily_count[d] = 0
        
        # 종목/전략별 손익
        coin_profit = {}
        strategy_stats = {}
        strategy_profit = {}
        strategy_count = {}
        strategy_wins = {}
        
        for t in week_trades:
            d = t['timestamp'].date()
            daily_profit[d] = daily_profit.get(d, 0) + _paf(t)
            daily_count[d] = daily_count.get(d, 0) + 1
            
            coin = t['coin'].replace('KRW-', '')
            coin_profit[coin] = coin_profit.get(coin, 0) + _paf(t)
            buy_meta = t.get('buy_meta', {}) if isinstance(t.get('buy_meta'), dict) else {}
            strategy = str(t.get('strategy') or buy_meta.get('strategy') or 'UNKNOWN')
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {'trades': 0, 'wins': 0, 'profit': 0.0}
            strategy_stats[strategy]['trades'] += 1
            paf_strategy = _paf(t)
            strategy_stats[strategy]['profit'] += float(paf_strategy or 0)
            if paf_strategy > 0:
                strategy_stats[strategy]['wins'] += 1
            buy_meta = t.get("buy_meta", {}) if isinstance(t.get("buy_meta"), dict) else {}
            strategy = str(t.get("strategy") or buy_meta.get("strategy") or "UNKNOWN")
            strategy_profit[strategy] = strategy_profit.get(strategy, 0.0) + _paf(t)
            strategy_count[strategy] = strategy_count.get(strategy, 0) + 1
            if _paf(t) > 0:
                strategy_wins[strategy] = strategy_wins.get(strategy, 0) + 1
        
        top_winners = sorted(coin_profit.items(), key=lambda kv: kv[1], reverse=True)[:3]
        top_losers = sorted(coin_profit.items(), key=lambda kv: kv[1])[:3]
        
        best_coin = best['coin'].replace('KRW-', '')
        worst_coin = worst['coin'].replace('KRW-', '')
        
        message = f"""📆 <b>주간 리포트</b>

기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}

📊 거래: {len(week_trades)}회
✅ 승: {len(wins)}회
❌ 패: {len(losses)}회
📈 승률: {win_rate:.1f}%

💰 총 손익: {total_profit:+,.0f}원
💰 총 손익(수수료 반영): {total_profit_after_fees:+,.0f}원
💸 수수료(기간): {total_fee_sum:,.0f}원 (매수 {buy_fee_sum:,.0f} + 매도 {sell_fee_sum:,.0f})
거래대금(왕복): {turnover_krw:,.0f}원
수수료/거래대금: {fee_turnover_str}
💸 누적 수수료(세션): {self.stats.get_total_fees_krw():,.0f}원

📅 <b>일자별 손익</b>"""
        
        for d in sorted(daily_profit.keys()):
            pnl = daily_profit[d]
            cnt = daily_count.get(d, 0)
            message += f"\n{d.strftime('%m-%d')}: {pnl:+,.0f}원 ({cnt}회)"
        
        message += (
            f"\n\n🏆 최고: {best_coin} {_paf(best):+,.0f}원"
            f"\n📉 최악: {worst_coin} {_paf(worst):+,.0f}원"
        )
        
        if top_winners:
            message += "\n\n📈 <b>종목 상위</b>"
            for coin, pnl in top_winners:
                message += f"\n{coin}: {pnl:+,.0f}원"
        
        if top_losers:
            message += "\n\n📉 <b>종목 하위</b>"
            for coin, pnl in top_losers:
                message += f"\n{coin}: {pnl:+,.0f}원"

        if strategy_count:
            message += "\n\n🧠 <b>전략별 성과</b>"
            ranked_strategy = sorted(strategy_profit.items(), key=lambda kv: kv[1], reverse=True)
            for strategy, pnl in ranked_strategy:
                cnt = strategy_count.get(strategy, 0)
                win = strategy_wins.get(strategy, 0)
                wr = (win / cnt * 100) if cnt > 0 else 0
                message += f"\n{strategy}: {pnl:+,.0f}원 ({cnt}회, 승률 {wr:.1f}%)"
        
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
/weekly - 주간 리포트(최근 7일)
/positions - 보유 포지션
/balance - 잔고 확인

🎮 <b>제어</b>
/pause - 일시 정지
/resume - 거래 재개

ℹ️ <b>기타</b>
/version - 버전 정보

❓ /help - 이 도움말
"""
        
        self.telegram.send_message(message)
    
    def _telegram_version(self):
        """텔레그램: 버전 정보"""
        self.telegram.send_message(
            f"ℹ️ <b>버전 정보</b>\n\n"
            f"이름: {self.bot_display_name}\n"
            f"코드명: {self.bot_name}\n"
            f"버전: v{self.bot_version}"
        )
    
    def status(self):
        """현재 상태 표시"""
        
        status = self.stats.get_current_status()
        market_snapshot = self._get_market_snapshot(probe=True)
        market_lines = self._format_market_snapshot_lines(market_snapshot)
        
        print("\n" + "="*80)
        print("📊 현재 거래 상태")
        print("="*80)
        
        if not self.is_running:
            print("⏸️  상태: 정지")
        else:
            print("▶️  상태: 실행 중")

        print("\n🌐 시장 상황")
        for line in market_lines:
            print(f"  {line}")
        
        print(f"\n💰 자금 현황")
        print(f"  초기 자금: {status['initial_balance']:,.0f}원")
        print(f"  현재 잔고: {status['current_balance']:,.0f}원")
        
        # 사용 가능 금액 계산
        invested_amount = sum(pos['buy_price'] * pos['amount'] for pos in self.stats.positions.values())
        available_investment = max(
            0.0,
            min(
                self.max_total_investment - invested_amount,
                status['current_balance']
            )
        )
        
        print(f"  투자 중: {invested_amount:,.0f}원")
        print(f"  사용 가능: {available_investment:,.0f}원 (한도: {self.max_total_investment:,.0f}원)")
        print(f"  총 평가액: {status['total_value']:,.0f}원")
        print(f"  총 수익률: {status['total_return']:+.2f}%")
        print(f"  총 손익: {status['total_profit_krw']:+,.0f}원")
        if 'total_profit_after_fees_krw' in status:
            print(f"  총 손익(수수료 반영): {status.get('total_profit_after_fees_krw', 0):+,.0f}원")

        fee_rate = getattr(self.engine, "FEE", 0.0005)
        positions_value = max(0.0, float(status.get('total_value', 0) or 0) - float(status.get('current_balance', 0) or 0))
        est_exit_fee = positions_value * fee_rate

        print(f"\n💸 수수료(추정) (수수료율 {fee_rate*100:.3f}%)")
        print(f"  누적 수수료(세션): {status.get('total_fees_krw', 0):,.0f}원")
        print(f"  예상 청산 수수료: {est_exit_fee:,.0f}원")
        print(f"  평가액(청산수수료 차감): {status['total_value'] - est_exit_fee:,.0f}원")
        
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

        fee_rate = getattr(self.engine, "FEE", 0.0005)
        buy_fee_sum = 0.0
        sell_fee_sum = 0.0
        profit_after_fees_sum = 0.0
        turnover_krw = 0.0

        def _paf_day(tr):
            paf = tr.get('profit_after_fees_krw', None)
            if paf is not None:
                return float(paf or 0)
            try:
                buy_price = float(tr.get('buy_price', 0) or 0)
                amount = float(tr.get('amount', 0) or 0)
                buy_fee = float(tr.get('buy_fee_krw', 0) or 0)
                if buy_fee <= 0:
                    buy_fee = buy_price * amount * fee_rate
                return float(tr.get('profit_krw', 0) or 0) - buy_fee
            except Exception:
                return float(tr.get('profit_krw', 0) or 0)

        for t in today_trades:
            try:
                buy_price = float(t.get('buy_price', 0) or 0)
                sell_price = float(t.get('sell_price', 0) or 0)
                amount = float(t.get('amount', 0) or 0)
                turnover_krw += (buy_price * amount) + (sell_price * amount)

                buy_fee = t.get('buy_fee_krw', None)
                sell_fee = t.get('sell_fee_krw', None)

                if buy_fee is None:
                    buy_fee = buy_price * amount * fee_rate
                if sell_fee is None:
                    sell_fee = sell_price * amount * fee_rate

                buy_fee = float(buy_fee or 0)
                sell_fee = float(sell_fee or 0)

                buy_fee_sum += buy_fee
                sell_fee_sum += sell_fee

                paf = t.get('profit_after_fees_krw', None)
                if paf is None:
                    paf = float(t.get('profit_krw', 0) or 0) - buy_fee
                profit_after_fees_sum += float(paf or 0)
            except Exception:
                continue

        total_fee_sum = buy_fee_sum + sell_fee_sum
        
        best_trade = max(today_trades, key=lambda x: x['profit_rate'])
        worst_trade = min(today_trades, key=lambda x: x['profit_rate'])
        
        # 코인별 통계
        coin_profits = {}
        strategy_stats = {}
        for trade in today_trades:
            coin = trade['coin'].replace('KRW-', '')
            if coin not in coin_profits:
                coin_profits[coin] = {'trades': 0, 'profit': 0}
            coin_profits[coin]['trades'] += 1
            paf = trade.get('profit_after_fees_krw', None)
            if paf is None:
                try:
                    buy_fee = float(trade.get('buy_fee_krw', 0) or 0)
                    if buy_fee <= 0:
                        buy_fee = float(trade.get('buy_price', 0) or 0) * float(trade.get('amount', 0) or 0) * fee_rate
                except Exception:
                    buy_fee = 0.0
                paf = float(trade.get('profit_krw', 0) or 0) - buy_fee
            coin_profits[coin]['profit'] += float(paf or 0)

            buy_meta = trade.get('buy_meta', {}) if isinstance(trade.get('buy_meta'), dict) else {}
            strategy = str(trade.get('strategy') or buy_meta.get('strategy') or 'UNKNOWN')
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {'trades': 0, 'wins': 0, 'profit': 0.0}
            strategy_stats[strategy]['trades'] += 1
            paf_strategy = _paf_day(trade)
            strategy_stats[strategy]['profit'] += float(paf_strategy or 0)
            if paf_strategy > 0:
                strategy_stats[strategy]['wins'] += 1
        
        # 출력
        print(f"\n📊 오늘 ({today.strftime('%Y-%m-%d')})")
        print(f"  총 거래: {total_trades}회")
        print(f"  승/패: {wins}승 {losses}패")
        print(f"  승률: {win_rate:.1f}%")
        
        print(f"\n💰 수익 현황")
        print(f"  총 손익: {total_profit:+,.0f}원")
        print(f"  평균 손익: {avg_profit:+,.0f}원")
        print(f"  총 손익(수수료 반영): {profit_after_fees_sum:+,.0f}원")
        print(f"\n💸 수수료(기간) (수수료율 {fee_rate*100:.3f}%)")
        print(f"  합계: {total_fee_sum:,.0f}원 (매수 {buy_fee_sum:,.0f}원 + 매도 {sell_fee_sum:,.0f}원)")
        if turnover_krw > 0:
            print(f"  거래대금(왕복): {turnover_krw:,.0f}원")
            print(f"  수수료/거래대금: {(total_fee_sum/turnover_krw*100):.3f}%")
        print(f"  누적 수수료(세션): {self.stats.get_total_fees_krw():,.0f}원")
        
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

        if strategy_stats:
            print(f"\n🧠 전략별 성과")
            sorted_strategies = sorted(strategy_stats.items(), key=lambda x: x[1]['profit'], reverse=True)
            for strategy, st in sorted_strategies:
                trades = st['trades']
                wins = st['wins']
                wr = (wins / trades * 100) if trades > 0 else 0
                print(f"  {strategy}: {st['profit']:+,.0f}원 | {trades}회 | 승률 {wr:.1f}%")
        
        print("="*80 + "\n")

    def weekly_stats(self):
        """주간 통계 표시 (최근 7일, 파일 기록 포함)"""

        print("\n" + "="*80)
        print("📆 주간 거래 통계 (최근 7일)")
        print("="*80)

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=6)

        # 파일 + 메모리 통합 (중복 제거: timestamp 기준)
        all_trades_dict = {}

        for i in range(7):
            d = start_date + timedelta(days=i)
            file_trades = self.stats.load_daily_trades(datetime.combine(d, datetime.min.time()))
            for t in file_trades:
                all_trades_dict[t['timestamp'].isoformat()] = t

        memory_trades = [
            t for t in self.stats.trades
            if start_date <= t['timestamp'].date() <= end_date
        ]
        for t in memory_trades:
            all_trades_dict[t['timestamp'].isoformat()] = t

        week_trades = list(all_trades_dict.values())

        if not week_trades:
            print("\n⚠️  최근 7일 거래 내역이 없습니다.")
            print(f"  기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
            print("="*80 + "\n")
            return

        fee_rate = getattr(self.engine, "FEE", 0.0005)

        # 수수료 반영 손익(가능하면 trade_history의 profit_after_fees_krw 사용, 없으면 추정)
        def _paf(tr):
            paf = tr.get('profit_after_fees_krw', None)
            if paf is not None:
                return float(paf or 0)
            try:
                bp = float(tr.get('buy_price', 0) or 0)
                amt = float(tr.get('amount', 0) or 0)
                bf = float(tr.get('buy_fee_krw', 0) or 0)
                if bf <= 0:
                    bf = bp * amt * fee_rate
                return float(tr.get('profit_krw', 0) or 0) - bf
            except Exception:
                return float(tr.get('profit_krw', 0) or 0)

        buy_fee_sum = 0.0
        sell_fee_sum = 0.0
        total_profit = 0.0
        total_profit_after_fees = 0.0
        turnover_krw = 0.0

        for t in week_trades:
            try:
                total_profit += float(t.get('profit_krw', 0) or 0)

                buy_price = float(t.get('buy_price', 0) or 0)
                sell_price = float(t.get('sell_price', 0) or 0)
                amount = float(t.get('amount', 0) or 0)
                turnover_krw += (buy_price * amount) + (sell_price * amount)

                buy_fee = t.get('buy_fee_krw', None)
                sell_fee = t.get('sell_fee_krw', None)

                if buy_fee is None:
                    buy_fee = buy_price * amount * fee_rate
                if sell_fee is None:
                    sell_fee = sell_price * amount * fee_rate

                buy_fee = float(buy_fee or 0)
                sell_fee = float(sell_fee or 0)

                buy_fee_sum += buy_fee
                sell_fee_sum += sell_fee

                paf = t.get('profit_after_fees_krw', None)
                if paf is None:
                    paf = float(t.get('profit_krw', 0) or 0) - buy_fee
                total_profit_after_fees += float(paf or 0)
            except Exception:
                continue

        wins = [t for t in week_trades if _paf(t) > 0]
        losses = [t for t in week_trades if _paf(t) <= 0]
        win_rate = (len(wins) / len(week_trades) * 100) if week_trades else 0

        total_fee_sum = buy_fee_sum + sell_fee_sum

        best = max(week_trades, key=_paf)
        worst = min(week_trades, key=_paf)

        # 일자별 손익/횟수
        daily_profit = {}
        daily_count = {}
        for i in range(7):
            d = start_date + timedelta(days=i)
            daily_profit[d] = 0
            daily_count[d] = 0

        # 종목/전략별 손익
        coin_profit = {}
        strategy_stats = {}

        for t in week_trades:
            d = t['timestamp'].date()
            daily_profit[d] = daily_profit.get(d, 0) + _paf(t)
            daily_count[d] = daily_count.get(d, 0) + 1

            coin = t['coin'].replace('KRW-', '')
            coin_profit[coin] = coin_profit.get(coin, 0) + _paf(t)
            buy_meta = t.get('buy_meta', {}) if isinstance(t.get('buy_meta'), dict) else {}
            strategy = str(t.get('strategy') or buy_meta.get('strategy') or 'UNKNOWN')
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {'trades': 0, 'wins': 0, 'profit': 0.0}
            strategy_stats[strategy]['trades'] += 1
            paf_strategy = _paf(t)
            strategy_stats[strategy]['profit'] += float(paf_strategy or 0)
            if paf_strategy > 0:
                strategy_stats[strategy]['wins'] += 1

        top_winners = sorted(coin_profit.items(), key=lambda kv: kv[1], reverse=True)[:3]
        top_losers = sorted(coin_profit.items(), key=lambda kv: kv[1])[:3]

        best_coin = best['coin'].replace('KRW-', '')
        worst_coin = worst['coin'].replace('KRW-', '')

        print(f"\n📅 기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
        print(f"📊 거래: {len(week_trades)}회")
        print(f"✅ 승: {len(wins)}회")
        print(f"❌ 패: {len(losses)}회")
        print(f"📈 승률: {win_rate:.1f}%")

        print(f"\n💰 총 손익: {total_profit:+,.0f}원")
        print(f"💰 총 손익(수수료 반영): {total_profit_after_fees:+,.0f}원")
        print(f"\n💸 수수료(기간) (수수료율 {fee_rate*100:.3f}%)")
        print(f"  합계: {total_fee_sum:,.0f}원 (매수 {buy_fee_sum:,.0f}원 + 매도 {sell_fee_sum:,.0f}원)")
        if turnover_krw > 0:
            print(f"  거래대금(왕복): {turnover_krw:,.0f}원")
            print(f"  수수료/거래대금: {(total_fee_sum/turnover_krw*100):.3f}%")
        print(f"  누적 수수료(세션): {self.stats.get_total_fees_krw():,.0f}원")

        print(f"\n📅 일자별 손익")
        for d in sorted(daily_profit.keys()):
            pnl = daily_profit[d]
            cnt = daily_count.get(d, 0)
            print(f"  {d.strftime('%Y-%m-%d')}: {pnl:+,.0f}원 ({cnt}회)")

        print(f"\n🏆 최고 거래: {best_coin} {_paf(best):+,.0f}원")
        print(f"📉 최악 거래: {worst_coin} {_paf(worst):+,.0f}원")

        if top_winners:
            print(f"\n📈 종목 상위")
            for coin, pnl in top_winners:
                print(f"  {coin}: {pnl:+,.0f}원")

        if top_losers:
            print(f"\n📉 종목 하위")
            for coin, pnl in top_losers:
                print(f"  {coin}: {pnl:+,.0f}원")

        if strategy_stats:
            print(f"\n🧠 전략별 성과")
            sorted_strategies = sorted(strategy_stats.items(), key=lambda x: x[1]['profit'], reverse=True)
            for strategy, st in sorted_strategies:
                trades = st['trades']
                wins = st['wins']
                wr = (wins / trades * 100) if trades > 0 else 0
                print(f"  {strategy}: {st['profit']:+,.0f}원 | {trades}회 | 승률 {wr:.1f}%")

        print("="*80 + "\n")
    
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
    
    def _calculate_dynamic_investment(self, signal_score, available_krw=None, buy_meta=None, orderbook_details=None):
        """투자 금액 계산 (리스크 기반 추천 금액 전용)."""

        min_trade = float(self.config["trading"]["min_trade_amount"])

        current_investment = sum(
            pos["buy_price"] * pos["amount"]
            for pos in self.stats.positions.values()
        )
        cap_remaining = max(0.0, float(self.max_total_investment) - float(current_investment))
        if cap_remaining < min_trade:
            return 0.0

        if available_krw is None:
            try:
                available_krw = float(self.engine.get_balance("KRW") or 0)
            except Exception:
                available_krw = 0.0
        else:
            try:
                available_krw = float(available_krw or 0)
            except Exception:
                available_krw = 0.0

        tradable_budget = max(0.0, min(cap_remaining, available_krw))
        if tradable_budget < min_trade:
            return 0.0

        meta = buy_meta if isinstance(buy_meta, dict) else {}
        recommended = meta.get("recommended_invest_krw")
        try:
            recommended = float(recommended) if recommended is not None else 0.0
        except Exception:
            recommended = 0.0

        if recommended <= 0:
            return 0.0

        weight_remaining = meta.get("weight_remaining_krw")
        try:
            weight_remaining = float(weight_remaining) if weight_remaining is not None else None
        except Exception:
            weight_remaining = None
        if weight_remaining is not None and weight_remaining > 0:
            recommended = min(recommended, weight_remaining)

        investment = min(tradable_budget, recommended)
        if investment < min_trade:
            return 0.0

        return float(int(investment))

    def _estimate_total_value(self, cash_balance):
        """총자산(현금+포지션 평가액) 추정.

        - 호출 시점의 포지션 수는 보통 0~3개 수준이므로, 현재가 조회 비용은 제한적입니다.
        - 가격 조회 실패 시 매수가로 폴백합니다.
        """
        try:
            cash = float(cash_balance or 0)
        except Exception:
            cash = 0.0

        total = cash
        for coin, pos in list(self.stats.positions.items()):
            try:
                price = self.engine.get_current_price(coin)
                if not price:
                    price = float(pos.get('buy_price', 0) or 0)
                amount = float(pos.get('amount', 0) or 0)
                total += float(price) * amount
            except Exception:
                continue

        return float(total)

    def _emit_analysis_heartbeat(self, daily_profit_krw=None, daily_profit_pct=None):
        """주기적 운영 상태 로그(분석용)."""
        now = datetime.now()
        if self._last_analysis_heartbeat_at:
            elapsed = (now - self._last_analysis_heartbeat_at).total_seconds()
            if elapsed < (self.analysis_heartbeat_minutes * 60):
                return
        self._last_analysis_heartbeat_at = now

        status = self.stats.get_current_status()
        invested = sum(pos['buy_price'] * pos['amount'] for pos in self.stats.positions.values())
        available = max(0.0, min(self.max_total_investment - invested, status['current_balance']))

        payload = {
            "ts": now.isoformat(),
            "global_regime": getattr(self.engine, "global_regime", "RANGE"),
            "target_coins": list(self.target_coins),
            "positions_count": int(len(self.stats.positions)),
            "positions": [
                {
                    "ticker": t,
                    "amount": float(p.get("amount", 0) or 0),
                    "buy_price": float(p.get("buy_price", 0) or 0),
                }
                for t, p in self.stats.positions.items()
            ],
            "capital": {
                "max_total_investment_krw": float(self.max_total_investment),
                "invested_krw": float(invested),
                "available_krw": float(available),
                "cash_krw": float(status.get("current_balance", 0) or 0),
                "total_value_krw": float(status.get("total_value", 0) or 0),
            },
            "performance": {
                "total_return_pct": float(status.get("total_return", 0) or 0),
                "total_trades": int(status.get("total_trades", 0) or 0),
                "win_rate_pct": float(status.get("win_rate", 0) or 0),
                "daily_profit_krw": float(daily_profit_krw if daily_profit_krw is not None else 0),
                "daily_profit_pct": float(daily_profit_pct if daily_profit_pct is not None else 0),
                "total_fees_krw": float(status.get("total_fees_krw", 0) or 0),
            },
            "state": {
                "running": bool(self.is_running),
                "trading_paused": bool(self.is_trading_paused),
                "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            },
        }
        self.logger.log_decision("LOOP_HEARTBEAT", payload)
    
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
                    
                    # 일시 정지 중이면 대기
                    if self.is_trading_paused:
                        time.sleep(60)  # 1분마다 체크
                        continue

                # 글로벌 레짐 주기 갱신
                try:
                    _, regime_payload = self.engine.update_global_regime(force=False)

                    # 레짐 전환이 실제 확정된 경우에만 시장 변화 알림 전송
                    if isinstance(regime_payload, dict):
                        applied = bool(regime_payload.get("applied", False))
                        prev_regime = str(regime_payload.get("previous", ""))
                        curr_regime = str(regime_payload.get("current", ""))
                        if applied and prev_regime and curr_regime and prev_regime != curr_regime:
                            sent = self.telegram.notify_market_change(
                                previous_regime=prev_regime,
                                current_regime=curr_regime,
                                detect_meta=regime_payload.get("detect", {}) or {},
                                confirm_count=regime_payload.get("confirm_count"),
                            )
                            if sent:
                                self.logger.info(
                                    f"텔레그램: 시장 상황 변경 알림 전송 ({prev_regime} -> {curr_regime})"
                                )
                except Exception as e:
                    self.logger.warning(f"⚠️ 레짐 갱신 오류: {e}")

                # 주기적 분석 로그(운영 상태 스냅샷)
                try:
                    self._emit_analysis_heartbeat(
                        daily_profit_krw=daily_profit,
                        daily_profit_pct=daily_profit_pct,
                    )
                except Exception as e:
                    self.logger.warning(f"⚠️ 분석 heartbeat 기록 오류: {e}")
                
                # 거래 대상이 비어있으면 고정 종목을 재적용
                if not self.target_coins:
                    self.logger.warning("⚠️ 거래 대상이 비어 있어 고정 종목을 재적용합니다.")
                    self._apply_fixed_target_coins(reason="empty_recover")

                    # 대상 종목도 없고 보유 포지션도 없으면 대기만 하고 루프 종료
                    if not self.stats.positions:
                        time.sleep(self.check_interval)
                        continue
                
                # 각 코인별로 매매 체크
                # 보유 포지션은 대상 목록에서 제외되더라도 항상 매도 신호를 체크해야 함
                tickers_to_check = list(self.target_coins)
                for held in list(self.stats.positions.keys()):
                    if held not in tickers_to_check:
                        tickers_to_check.append(held)

                for ticker in tickers_to_check:
                    
                    # 포지션 없을 때 - 매수 검토
                    if ticker not in self.stats.positions:
                        
                        # 중복 매수 방지 1차 체크
                        with self.buy_lock:
                            # 1단계: 매수 진행 중 체크
                            if ticker in self.buying_in_progress:
                                self.logger.debug(f"  {ticker} 이미 매수 진행 중 - 건너뜀")
                                continue
                            
                            # 2단계: 포지션 재확인 (Race Condition 방지)
                            if ticker in self.stats.positions:
                                self.logger.debug(f"  {ticker} 이미 포지션 보유 중 - 건너뜀")
                                continue
                            
                            # 3단계: 실제 잔고 확인 (유령 포지션 방지)
                            coin = ticker.split('-')[1]
                            actual_balance = self.engine.upbit.get_balance(coin)
                            if actual_balance > 0:
                                # 최소 주문금액 미만의 잔고(dust)는 매수 차단에서 제외
                                current_price = self.engine.get_current_price(ticker)
                                if current_price:
                                    balance_value = actual_balance * current_price
                                    min_trade = self.config['trading']['min_trade_amount']
                                    
                                    if balance_value < min_trade:
                                        self.logger.debug(
                                            f"  {ticker} 소액 잔고 무시: {actual_balance:.8f} "
                                            f"({balance_value:,.0f}원 < {min_trade:,.0f}원)"
                                        )
                                        actual_balance = 0
                                
                            if actual_balance > 0:
                                handled = self._handle_untracked_balance(ticker, actual_balance, is_startup=False)
                                if handled:
                                    continue
                                
                                self.logger.warning(
                                    f"  ⚠️  {ticker} 실제 잔고 존재 ({actual_balance:.8f}), 매수 취소"
                                )
                                continue
                        
                        # 손절 직후 동일 종목 재진입 방지
                        if self.reentry_cooldown_after_stoploss_minutes > 0 and self._is_reentry_cooldown_active(ticker):
                            continue

                        # 동시 포지션 제한
                        if len(self.stats.positions) >= self.max_coins:
                            continue
                        
                        # 매수 신호 확인
                        buy_signal, signals, current_price, signal_score, buy_meta = self.engine.check_buy_signal(ticker)

                        # 분석 로그: 차단 사유를 변경 시점마다 기록 (중복 로그 억제)
                        if (not buy_signal) and isinstance(buy_meta, dict) and buy_meta.get("blocked_by"):
                            blocked_by = sorted(list(set(buy_meta.get("blocked_by") or [])))
                            signature = (
                                str(buy_meta.get("candle_ts") or ""),
                                tuple(blocked_by),
                                int(signal_score or 0),
                                str(getattr(self.engine, "global_regime", "RANGE")),
                            )
                            if self._last_buy_block_signature.get(ticker) != signature:
                                self._last_buy_block_signature[ticker] = signature
                                self.logger.log_decision(
                                    "BUY_BLOCKED",
                                    {
                                        "ticker": ticker,
                                        "global_regime": getattr(self.engine, "global_regime", "RANGE"),
                                        "score": int(signal_score or 0),
                                        "signals": list(signals),
                                        "blocked_by": blocked_by,
                                        "meta": buy_meta,
                                    },
                                )
                        
                        if buy_signal and current_price:
                            candle_ts = str((buy_meta or {}).get("candle_ts") or "")
                            if candle_ts:
                                if self.last_buy_attempt_candle.get(ticker) == candle_ts:
                                    self.logger.debug(f"  {ticker} 동일 확정봉 재시도 스킵: {candle_ts}")
                                    continue
                                self.last_buy_attempt_candle[ticker] = candle_ts

                            # 분석 로그 (매수 시그널 발생)
                            self.logger.log_decision(
                                "BUY_SIGNAL",
                                {
                                    "ticker": ticker,
                                    "current_price": float(current_price),
                                    "signals": list(signals),
                                    "score": int(signal_score),
                                    "meta": buy_meta or {},
                                },
                            )

                            # 호가창 안전성 체크
                            is_safe, safety_msg, orderbook_details = self.engine.check_orderbook_safety(ticker)
                            if not is_safe:
                                self.logger.debug(f"  {ticker} 호가 불안정: {safety_msg}")
                                self.logger.log_decision(
                                    "BUY_CANCELLED",
                                    {
                                        "ticker": ticker,
                                        "reason": f"orderbook_unsafe:{safety_msg}",
                                        "orderbook": orderbook_details or {},
                                        "meta": buy_meta or {},
                                    },
                                )
                                continue
                            
                            # 체결/슬리피지 분석용(매수 직전 스냅샷)
                            mid_price = None
                            try:
                                ask = float((orderbook_details or {}).get("ask_price", 0) or 0)
                                bid = float((orderbook_details or {}).get("bid_price", 0) or 0)
                                if ask > 0 and bid > 0:
                                    mid_price = (ask + bid) / 2
                            except Exception:
                                mid_price = None
                            
                            # 현재 잔고 기준으로 투자 금액 계산(한도/잔고 동시 반영)
                            raw_available_krw = self.engine.get_balance("KRW")
                            try:
                                available_krw = float(raw_available_krw or 0)
                            except Exception:
                                available_krw = 0.0
                            invest_amount = self._calculate_dynamic_investment(
                                signal_score,
                                available_krw=available_krw,
                                buy_meta=buy_meta,
                                orderbook_details=orderbook_details,
                            )

                            self.logger.log_decision(
                                "BUY_SIZING",
                                {
                                    "ticker": ticker,
                                    "global_regime": getattr(self.engine, "global_regime", "RANGE"),
                                    "score": int(signal_score),
                                    "available_krw": float(available_krw),
                                    "invest_amount_krw": float(invest_amount),
                                    "min_trade_krw": float(self.config['trading']['min_trade_amount']),
                                    "recommended_invest_krw": float((buy_meta or {}).get("recommended_invest_krw", 0) or 0),
                                    "risk_krw": float((buy_meta or {}).get("risk_krw", 0) or 0),
                                    "weight_remaining_krw": float((buy_meta or {}).get("weight_remaining_krw", 0) or 0),
                                    "total_cap_remaining_krw": float((buy_meta or {}).get("total_cap_remaining_krw", 0) or 0),
                                    "spread_pct": float((orderbook_details or {}).get("spread_pct", 0) or 0),
                                    "meta": buy_meta or {},
                                },
                            )
                            
                            if invest_amount >= self.config['trading']['min_trade_amount']:
                                if available_krw >= invest_amount:
                                    # 매수 진행 표시 (실제 매수 직전)
                                    with self.buy_lock:
                                        # 최종 재확인 (다른 스레드에서 이미 매수했을 수 있음)
                                        if ticker in self.buying_in_progress or ticker in self.stats.positions:
                                            self.logger.debug(f"  {ticker} 최종 체크 실패 - 건너뜀")
                                            continue
                                        
                                        self.buying_in_progress.add(ticker)
                                    
                                    try:
                                        # 매수 실행
                                        buy_result = self.engine.execute_buy(ticker, invest_amount)
                                        
                                        # 성공한 경우에만 기록
                                        if buy_result and 'price' in buy_result and 'amount' in buy_result:
                                            # 포지션 기록 (UUID 포함)
                                            self.stats.add_position(
                                                ticker,
                                                buy_result['price'],
                                                buy_result['amount'],
                                                buy_result.get('uuid'),
                                                buy_fee_krw=buy_result.get('fee', 0),
                                                buy_signals=signals,
                                                buy_score=signal_score,
                                                buy_meta=buy_meta,
                                            )
                                            self.logger.info(
                                                f"포지션 개설 확인: {ticker} "
                                                f"수량={float(buy_result.get('amount', 0) or 0):.8f} "
                                                f"가격={float(buy_result.get('price', 0) or 0):,.0f}"
                                            )
                                            
                                            # 잔고 업데이트
                                            new_balance = self.engine.get_balance("KRW")
                                            new_total_value = self._estimate_total_value(new_balance)
                                            self.stats.update_balance(new_balance, current_total_value=new_total_value)
                                            
                                            # 로그 기록 (점수 포함)
                                            signal_str = f"{', '.join(signals)} (점수:{signal_score})"
                                            self.logger.info(f"🔵 매수 완료 | {ticker} | {invest_amount:,.0f}원 | {signal_str}")
                                            
                                            # 텔레그램 알림 (실패 로깅)
                                            success = self.telegram.notify_buy(
                                                ticker,
                                                buy_result['price'],
                                                buy_result['amount'],
                                                invest_amount,
                                                signals,
                                                signal_score
                                            )
                                            
                                            if not success:
                                                self.logger.debug(f"  ⚠️  {ticker} 텔레그램 알림 전송 실패")
                                            
                                            self.logger.log_buy(
                                                ticker,
                                                buy_result['price'],
                                                buy_result['amount'],
                                                buy_result['total_krw'],
                                                buy_result['fee'],
                                                signals,
                                                new_balance
                                            )

                                            # 분석 로그 (매수 체결)
                                            self.logger.log_decision(
                                                "BUY_EXECUTED",
                                                {
                                                    "ticker": ticker,
                                                    "invest_amount_krw": float(invest_amount),
                                                    "price": float(buy_result.get("price", 0) or 0),
                                                    "amount": float(buy_result.get("amount", 0) or 0),
                                                    "fee_krw": float(buy_result.get("fee", 0) or 0),
                                                    "orderbook": orderbook_details or {},
                                                    "mid_price": float(mid_price) if mid_price else None,
                                                    "slippage_bps": (
                                                        float(buy_result.get("price", 0) or 0) / float(mid_price) - 1.0
                                                    ) * 10000
                                                    if mid_price
                                                    else None,
                                                    "signals": list(signals),
                                                    "score": int(signal_score),
                                                    "meta": buy_meta or {},
                                                },
                                            )
                                            
                                            # 수수료 누적(가능하면 실제, 없으면 추정)
                                            self.stats.add_fee(buy_result.get('fee', 0))
                                        else:
                                            # 매수 실패
                                            self.logger.warning(f"⚠️  {ticker} 매수 실패")
                                            self.logger.log_decision(
                                                "BUY_FAILED",
                                                {
                                                    "ticker": ticker,
                                                    "invest_amount_krw": float(invest_amount),
                                                    "meta": buy_meta or {},
                                                },
                                            )
                                    
                                    finally:
                                        # 매수 완료 (성공/실패 상관없이 제거)
                                        with self.buy_lock:
                                            self.buying_in_progress.discard(ticker)
                                else:
                                    self.logger.log_decision(
                                        "BUY_SKIPPED",
                                        {
                                            "ticker": ticker,
                                            "reason": "insufficient_krw",
                                            "available_krw": float(available_krw),
                                            "required_krw": float(invest_amount),
                                            "meta": buy_meta or {},
                                        },
                                    )
                            else:
                                self.logger.debug(f"  {ticker} 투자 한도 초과 또는 부족")
                                self.logger.log_decision(
                                    "BUY_SKIPPED",
                                    {
                                        "ticker": ticker,
                                        "reason": "below_min_trade",
                                        "invest_amount_krw": float(invest_amount),
                                        "min_trade_krw": float(self.config['trading']['min_trade_amount']),
                                        "meta": buy_meta or {},
                                    },
                                )
                    
                    # 포지션 있을 때 - 매도 검토
                    elif ticker in self.stats.positions:
                        position = self.stats.positions[ticker]
                        
                        should_sell, reason, sell_ratio, sell_meta = self.engine.check_sell_signal(ticker, position)
                        
                        if should_sell:
                            self.logger.log_decision(
                                "SELL_SIGNAL",
                                {
                                    "ticker": ticker,
                                    "reason": reason,
                                    "sell_ratio": float(sell_ratio),
                                    "position": {
                                        "buy_price": float(position.get("buy_price", 0) or 0),
                                        "amount": float(position.get("amount", 0) or 0),
                                        "highest_price": float(position.get("highest_price", 0) or 0),
                                        "timestamp": position.get("timestamp").isoformat() if position.get("timestamp") else None,
                                    },
                                    "meta": sell_meta or {},
                                },
                            )
                            # 매도 실행 (실제 잔고 기준, locked 자동 제외)
                            sell_result = self.engine.execute_sell(ticker, position, sell_ratio)
                            
                            # 성공한 경우에만 처리
                            if sell_result and 'price' in sell_result and 'amount' in sell_result:
                                # 수익 계산 (실제 매도 수량 기준)
                                buy_cost = position['buy_price'] * sell_result['amount']
                                profit_krw = sell_result['total_krw'] - buy_cost
                                profit_rate = ((sell_result['price'] - position['buy_price']) / position['buy_price']) * 100
                                if not isinstance(sell_meta, dict):
                                    sell_meta = {}
                                buy_meta = position.get("buy_meta", {}) if isinstance(position.get("buy_meta"), dict) else {}
                                stop_price = float(buy_meta.get("stop_price", 0) or 0)
                                risk_unit = (position['buy_price'] - stop_price) if stop_price > 0 else 0.0
                                if risk_unit > 0:
                                    realized_r = (sell_result['price'] - position['buy_price']) / risk_unit
                                    sell_meta.setdefault("stop_price", float(stop_price))
                                    sell_meta.setdefault("risk_unit", float(risk_unit))
                                    sell_meta["r_multiple"] = float(sell_meta.get("r_multiple", realized_r) or realized_r)
                                
                                # 잔고 업데이트
                                new_balance = self.engine.get_balance("KRW")
                                new_total_value = self._estimate_total_value(new_balance)
                                self.stats.update_balance(new_balance, current_total_value=new_total_value)
                                
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

                                # 수수료 누적(가능하면 실제, 없으면 추정)
                                self.stats.add_fee(sell_result.get('fee', 0))
                                
                                # 통계 업데이트
                                if sell_ratio >= 1.0:  # 전량 매도
                                    remaining_amount = sell_result.get('remaining_amount')
                                    if remaining_amount is None:
                                        remaining_amount = self.engine.get_tradable_balance(ticker)
                                    
                                    min_trade = self.config['trading']['min_trade_amount']
                                    ref_price = self.engine.get_current_price(ticker) or sell_result['price']
                                    remaining_value = remaining_amount * ref_price if ref_price else 0
                                    
                                    # 주문 가능 금액 이상의 잔량이 남으면 포지션 유지
                                    if remaining_amount > 0 and remaining_value >= min_trade:
                                        position['amount'] = remaining_amount
                                        self.stats.save_positions()
                                        self.logger.warning(
                                            f"⚠️ 전량 매도 후 잔량 남음: {ticker} | "
                                            f"{remaining_amount:.8f} ({remaining_value:,.0f}원) | 포지션 유지"
                                        )
                                        continue
                                    
                                    # 최소 주문금액 미만 잔량은 dust로 간주하고 포지션 종료
                                    if remaining_amount > 0:
                                        self.logger.info(
                                            f"💤 전량 매도 후 소액 잔량(dust): {ticker} | "
                                            f"{remaining_amount:.8f} ({remaining_value:,.0f}원)"
                                        )
                                    
                                    self.stats.remove_position(
                                        ticker,
                                        sell_result['price'],
                                        profit_krw,
                                        reason,
                                        sell_fee_krw=sell_result.get('fee', 0),
                                        sell_meta=sell_meta,
                                    )
                                    
                                    # 손절이면 동일 종목 재진입 쿨다운 적용
                                    if "손절" in str(reason):
                                        self._set_reentry_cooldown(
                                            ticker,
                                            self.reentry_cooldown_after_stoploss_minutes,
                                            reason
                                        )
                                    
                                    # 전량 매도 시에만 텔레그램 알림
                                    holding_time = (datetime.now() - position['timestamp']).total_seconds()
                                    success = self.telegram.notify_sell(
                                        ticker,
                                        position['buy_price'],
                                        sell_result['price'],
                                        profit_rate,
                                        profit_krw,
                                        holding_time,
                                        reason
                                    )
                                    
                                    if not success:
                                        self.logger.debug(f"  ⚠️  {ticker} 텔레그램 알림 전송 실패")
                                    
                                    self.logger.info(
                                        f"🔴 매도 완료 | {ticker} | "
                                        f"수익률 {profit_rate:+.2f}% | 손익 {profit_krw:+,.0f}원 | "
                                        f"{reason}"
                                    )

                                    self.logger.log_decision(
                                        "SELL_EXECUTED",
                                        {
                                            "ticker": ticker,
                                            "reason": reason,
                                            "sell_ratio": float(sell_ratio),
                                            "price": float(sell_result.get("price", 0) or 0),
                                            "amount": float(sell_result.get("amount", 0) or 0),
                                            "net_krw": float(sell_result.get("total_krw", 0) or 0),
                                            "fee_krw": float(sell_result.get("fee", 0) or 0),
                                            "profit_krw": float(profit_krw),
                                            "profit_rate": float(profit_rate),
                                            "meta": sell_meta or {},
                                        },
                                    )
                                
                                else:  # 분할 매도
                                    # 포지션 수량 감소
                                    position['amount'] -= sell_result['amount']
                                    
                                    # 스냅샷 즉시 업데이트 (중요!)
                                    self.stats.save_positions()

                                    # 분할 매도도 텔레그램 알림 전송
                                    holding_time = (datetime.now() - position['timestamp']).total_seconds()
                                    partial_reason = (
                                        f"{reason} | 부분청산 {sell_ratio*100:.0f}% "
                                        f"(잔여 {position['amount']:.8f})"
                                    )
                                    success = self.telegram.notify_sell(
                                        ticker,
                                        position['buy_price'],
                                        sell_result['price'],
                                        profit_rate,
                                        profit_krw,
                                        holding_time,
                                        partial_reason
                                    )
                                    if not success:
                                        self.logger.debug(f"  ⚠️  {ticker} 분할 매도 텔레그램 알림 전송 실패")
                                    
                                    self.logger.info(
                                        f"  ✅ 분할 매도: {sell_ratio*100:.0f}% | "
                                        f"매도수량 {sell_result['amount']:.8f} | "
                                        f"남은수량 {position['amount']:.8f} | "
                                        f"수익 {profit_krw:+,.0f}원"
                                    )
                                    
                                    # 남은 수량이 너무 작으면 전량 청산
                                    current_price = self.engine.get_current_price(ticker)
                                    if current_price and (position['amount'] * current_price < 5500):
                                        self.logger.info(f"  💸 잔여 수량 소액으로 전량 청산: {ticker}")
                                        final_sell = self.engine.execute_sell(ticker, position, 1.0)
                                        
                                        if final_sell:
                                            self.stats.add_fee(final_sell.get('fee', 0))
                                            final_profit = final_sell['total_krw'] - (position['buy_price'] * position['amount'])
                                            holding_time = (datetime.now() - position['timestamp']).total_seconds()
                                            self.stats.remove_position(
                                                ticker,
                                                final_sell['price'],
                                                final_profit,
                                                "소액청산",
                                                sell_fee_krw=final_sell.get('fee', 0),
                                                sell_meta={"note": "소액청산"},
                                            )

                                            success = self.telegram.notify_sell(
                                                ticker,
                                                position['buy_price'],
                                                final_sell['price'],
                                                ((final_sell['price'] - position['buy_price']) / position['buy_price']) * 100
                                                if position['buy_price'] > 0
                                                else 0.0,
                                                final_profit,
                                                holding_time,
                                                "소액청산(잔여 정리)"
                                            )
                                            if not success:
                                                self.logger.debug(f"  ⚠️  {ticker} 소액청산 텔레그램 알림 전송 실패")
                                            
                                            self.logger.info(
                                                f"  ✅ 소액청산 완료: {ticker} | "
                                                f"{final_profit:+,.0f}원"
                                            )
                
                # 대기
                time.sleep(self.check_interval)
                
            except Exception as e:
                self.logger.log_error("거래 루프 오류", e)
                time.sleep(self.check_interval)
        
        self.logger.info("🔄 거래 루프 종료")


def print_help():
    """도움말 출력"""
    print("\n" + "="*80)
    print(f"ℹ️ 버전: {BOT_NAME} v{BOT_VERSION}")
    print("="*80)
    print("📖 사용 가능한 명령어")
    print("="*80)
    print("  start  / 시작  - 트레이딩 시작 (고정 종목 기준 자동 매매 시작)")
    print("  stop   / 정지  - 트레이딩 정지 (모든 포지션 청산)")
    print("  status / 상태  - 현재 거래 상태 및 통계 표시")
    print("  daily  / 일일  - 오늘의 거래 통계 표시")
    print("  weekly / 주간  - 최근 7일 거래 통계 표시")
    print("  version / 버전 - 버전 정보 표시")
    print("  help   / 도움말 - 도움말 표시")
    print("  exit   / 종료  - 프로그램 종료")
    print("")
    print("💡 Tip: 위/아래 방향키로 이전 명령어를 불러올 수 있습니다")
    print("="*80 + "\n")


def main():
    """메인 함수"""
    
    print("="*80)
    print(f"🤖 {BOT_DISPLAY_NAME} v{BOT_VERSION}")
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
    
    # 자동 시작
    if bot.auto_start_on_launch:
        print("🚀 auto_start_on_launch=true, 트레이딩을 자동 시작합니다...")
        bot.start()
    
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
            
            if command in ('start', '시작'):
                bot.start()

            elif command in ('stop', '정지'):
                bot.stop()

            elif command in ('status', '상태'):
                bot.status()

            elif command in ('daily', '일일'):
                bot.daily_stats()

            elif command in ('weekly', '주간'):
                bot.weekly_stats()

            elif command in ('version', '버전'):
                print(f"ℹ️ {BOT_NAME} v{BOT_VERSION}")

            elif command in ('help', '도움말'):
                print_help()

            elif command in ('exit', 'quit', '종료'):
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
