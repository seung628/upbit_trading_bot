"""
로깅 모듈 - 시간별 로테이션 및 상세 거래 로그
"""

import logging
from logging.handlers import TimedRotatingFileHandler
import os
from datetime import datetime
import json


class TradingLogger:
    def __init__(self, config):
        self.config = config
        self.log_dir = config['logging']['log_dir']
        self.max_backup_count = int(config.get('logging', {}).get('max_backup_count', 30) or 30)
        
        # 로그 디렉토리 생성
        os.makedirs(self.log_dir, exist_ok=True)
        
        # 메인 로거 설정
        self.logger = self._setup_main_logger()
        
        # 거래 전용 로거 설정
        self.trade_logger = self._setup_trade_logger()
        
        # 통계 로거 설정
        self.stats_logger = self._setup_stats_logger()

        # 의사결정/분석 전용 로거(JSONL)
        self.decision_logger = self._setup_decision_logger()
    
    def _setup_main_logger(self):
        """메인 시스템 로거"""
        logger = logging.getLogger('TradingBot')
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # 재초기화 시 핸들러 중복 방지
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        
        # 콘솔 핸들러
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, self.config['logging']['console_log_level']))
        console_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_format)
        
        # 파일 핸들러 (시간별 로테이션)
        file_handler = TimedRotatingFileHandler(
            filename=os.path.join(self.log_dir, 'trading_bot.log'),
            when='H',
            interval=self.config['logging']['rotation_hours'],
            backupCount=self.max_backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, self.config['logging']['file_log_level']))
        file_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] [%(funcName)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        
        return logger
    
    def _setup_trade_logger(self):
        """거래 전용 로거 (CSV 형식)"""
        logger = logging.getLogger('TradeLog')
        logger.setLevel(logging.INFO)
        logger.propagate = False

        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        
        # 거래 로그 파일 핸들러
        trade_handler = TimedRotatingFileHandler(
            filename=os.path.join(self.log_dir, 'trades.log'),
            when='H',
            interval=self.config['logging']['rotation_hours'],
            backupCount=self.max_backup_count,
            encoding='utf-8'
        )
        trade_format = logging.Formatter('%(message)s')
        trade_handler.setFormatter(trade_format)
        
        logger.addHandler(trade_handler)
        
        # 헤더 작성 (파일이 비어있을 때만)
        log_file = os.path.join(self.log_dir, 'trades.log')
        if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
            logger.info("timestamp,type,coin,price,amount,total_krw,fee,profit_rate,profit_krw,reason,balance_krw,signals")
        
        return logger
    
    def _setup_stats_logger(self):
        """통계 로거"""
        logger = logging.getLogger('StatsLog')
        logger.setLevel(logging.INFO)
        logger.propagate = False

        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        
        stats_handler = TimedRotatingFileHandler(
            filename=os.path.join(self.log_dir, 'statistics.log'),
            when='D',
            interval=1,
            backupCount=self.max_backup_count,
            encoding='utf-8'
        )
        stats_format = logging.Formatter('%(message)s')
        stats_handler.setFormatter(stats_format)
        
        logger.addHandler(stats_handler)
        
        return logger

    def _setup_decision_logger(self):
        """의사결정/분석 전용 로거(JSONL)."""
        logger = logging.getLogger('DecisionLog')
        logger.setLevel(logging.INFO)
        logger.propagate = False

        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        decision_handler = TimedRotatingFileHandler(
            filename=os.path.join(self.log_dir, 'decisions.log'),
            when='D',
            interval=1,
            backupCount=self.max_backup_count,
            encoding='utf-8'
        )
        decision_format = logging.Formatter('%(message)s')
        decision_handler.setFormatter(decision_format)

        logger.addHandler(decision_handler)
        return logger

    def log_decision(self, event, payload=None):
        """의사결정/거래 이벤트(JSONL) 기록.

        event: 문자열 (예: BUY_SIGNAL, BUY_EXECUTED, SELL_SIGNAL, SELL_EXECUTED, COIN_REFRESH ...)
        payload: dict (JSON 직렬화 가능한 값)
        """
        try:
            record = {
                "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "event": str(event),
                "payload": payload or {},
            }
            self.decision_logger.info(json.dumps(record, ensure_ascii=False))
        except Exception:
            # 분석 로그는 실패해도 트레이딩에 영향 주지 않도록 무시
            pass
    
    def log_buy(self, coin, price, amount, total_krw, fee, signals, balance_krw):
        """매수 로그"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 상세 로그
        self.logger.info(f"🔵 매수 | {coin} | 가격: {price:,.0f}원 | "
                        f"수량: {amount:.8f} | 총액: {total_krw:,.0f}원 | "
                        f"신호: {', '.join(signals)}")
        
        # CSV 로그
        self.trade_logger.info(
            f"{timestamp},BUY,{coin},{price},{amount},{total_krw},{fee},0,0,"
            f"{';'.join(signals)},{balance_krw},\"{','.join(signals)}\""
        )
    
    def log_sell(self, coin, price, amount, total_krw, fee, profit_rate, profit_krw, reason, balance_krw):
        """매도 로그"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 상세 로그
        profit_emoji = "📈" if profit_krw > 0 else "📉"
        self.logger.info(f"🔴 매도 | {coin} | 가격: {price:,.0f}원 | "
                        f"수량: {amount:.8f} | 총액: {total_krw:,.0f}원 | "
                        f"수익률: {profit_rate:+.2f}% | "
                        f"수익금: {profit_emoji} {profit_krw:+,.0f}원 | "
                        f"사유: {reason}")
        
        # CSV 로그
        self.trade_logger.info(
            f"{timestamp},SELL,{coin},{price},{amount},{total_krw},{fee},"
            f"{profit_rate},{profit_krw},{reason},{balance_krw},"
        )
    
    def log_error(self, message, exception=None):
        """에러 로그"""
        if exception:
            self.logger.error(f"{message}: {str(exception)}", exc_info=True)
        else:
            self.logger.error(message)
    
    def log_daily_stats(self, stats):
        """일일 통계 로그"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        stats_json = json.dumps(stats, ensure_ascii=False)
        self.stats_logger.info(f"{timestamp}|{stats_json}")
        
        # 콘솔에도 표시
        self.logger.info("="*80)
        self.logger.info("📊 일일 통계")
        self.logger.info(f"  총 거래: {stats.get('total_trades', 0)}회")
        self.logger.info(f"  승: {stats.get('wins', 0)} / 패: {stats.get('losses', 0)}")
        self.logger.info(f"  승률: {stats.get('win_rate', 0):.1f}%")
        self.logger.info(f"  총 수익: {stats.get('total_profit_krw', 0):+,.0f}원")
        self.logger.info(f"  수익률: {stats.get('total_profit_rate', 0):+.2f}%")
        self.logger.info("="*80)
    
    def info(self, message):
        """일반 정보 로그"""
        self.logger.info(message)
    
    def warning(self, message):
        """경고 로그"""
        self.logger.warning(message)
    
    def debug(self, message):
        """디버그 로그"""
        self.logger.debug(message)
    
    def error(self, message):
        """에러 로그"""
        self.logger.error(message)
