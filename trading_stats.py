"""
거래 통계 및 상태 관리 모듈
"""

from datetime import datetime
from collections import defaultdict
import json
import os
import threading


class TradingStats:
    def __init__(self):
        # 스레드 안전성
        self.lock = threading.Lock()
        
        self.initial_balance = 0
        self.current_balance = 0
        
        # 거래 기록
        self.trades = []
        self.positions = {}  # {coin: {buy_price, amount, original_amount, timestamp, highest_price, uuid}}
        
        # 통계
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_profit_krw = 0
        self.total_fees = 0
        
        # 코인별 통계
        self.coin_stats = defaultdict(lambda: {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'profit_krw': 0
        })
        
        # 최대 손실
        self.max_drawdown = 0
        self.peak_balance = 0
        
        # 시작 시간
        self.start_time = None
        self.last_update = None
        
        # 히스토리 디렉토리
        self.history_dir = "trade_history"
        os.makedirs(self.history_dir, exist_ok=True)
        
        # 포지션 스냅샷 파일
        self.position_file = "positions_snapshot.json"
        
        # 일일 통계
        self.daily_start_balance = 0
        self.daily_trades_count = 0
    
    def start(self, initial_balance):
        """거래 시작"""
        with self.lock:
            self.initial_balance = initial_balance
            self.current_balance = initial_balance
            self.peak_balance = initial_balance
            self.daily_start_balance = initial_balance
            self.start_time = datetime.now()
            self.last_update = datetime.now()
    
    def add_position(self, coin, buy_price, amount, uuid=None):
        """포지션 추가"""
        with self.lock:
            self.positions[coin] = {
                'buy_price': buy_price,
                'amount': amount,
                'original_amount': amount,  # 원래 매수 수량 저장
                'timestamp': datetime.now(),
                'highest_price': buy_price,
                'uuid': uuid  # 주문 UUID 저장
            }
            self.save_positions()  # 포지션 저장
    
    def update_position_highest(self, coin, current_price):
        """포지션 최고가 업데이트"""
        with self.lock:
            if coin in self.positions:
                if current_price > self.positions[coin]['highest_price']:
                    self.positions[coin]['highest_price'] = current_price
                    self.save_positions()  # 변경 사항 저장
    
    def remove_position(self, coin, sell_price, profit_krw, reason):
        """포지션 제거 및 통계 업데이트"""
        with self.lock:
            if coin not in self.positions:
                return
            
            position = self.positions[coin]
            buy_price = position['buy_price']
            profit_rate = ((sell_price - buy_price) / buy_price) * 100
            
            # 거래 기록 저장
            now = datetime.now()
            trade_record = {
                'timestamp': now.isoformat(),
                'coin': coin,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'amount': position['amount'],
                'profit_rate': profit_rate,
                'profit_krw': profit_krw,
                'reason': reason,
                'holding_time': (now - position['timestamp']).total_seconds()
            }
            
            # 메모리에 저장
            self.trades.append({**trade_record, 'timestamp': now})
            
            # 파일에 영속화
            self._save_trade_to_file(trade_record)
            
            # 통계 업데이트
            self.total_trades += 1
            self.total_profit_krw += profit_krw
            
            if profit_krw > 0:
                self.wins += 1
                self.coin_stats[coin]['wins'] += 1
            else:
                self.losses += 1
                self.coin_stats[coin]['losses'] += 1
            
            self.coin_stats[coin]['trades'] += 1
            self.coin_stats[coin]['profit_krw'] += profit_krw
            
            # 포지션 제거
            del self.positions[coin]
            self.save_positions()  # 포지션 저장
            
            self.last_update = now
    
    def update_balance(self, current_balance):
        """잔고 업데이트 및 MDD 계산"""
        self.current_balance = current_balance
        
        # 최고점 갱신
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
        
        # MDD 계산
        if self.peak_balance > 0:
            drawdown = ((current_balance - self.peak_balance) / self.peak_balance) * 100
            if drawdown < self.max_drawdown:
                self.max_drawdown = drawdown
        
        self.last_update = datetime.now()
    
    def get_current_status(self):
        """현재 상태 조회"""
        total_value = self.current_balance
        
        # 보유 포지션 평가액 계산
        position_details = []
        for coin, pos in self.positions.items():
            position_details.append({
                'coin': coin,
                'buy_price': pos['buy_price'],
                'amount': pos['amount'],
                'buy_time': pos['timestamp']
            })
        
        # 전체 수익률
        if self.initial_balance > 0:
            total_return = ((total_value - self.initial_balance) / self.initial_balance) * 100
        else:
            total_return = 0
        
        # 승률
        win_rate = (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0
        
        # 평균 수익
        avg_profit = self.total_profit_krw / self.total_trades if self.total_trades > 0 else 0
        
        # 거래 시간
        if self.start_time:
            trading_duration = datetime.now() - self.start_time
            hours = trading_duration.total_seconds() / 3600
        else:
            hours = 0
        
        return {
            'initial_balance': self.initial_balance,
            'current_balance': self.current_balance,
            'total_value': total_value,
            'total_return': total_return,
            'total_profit_krw': self.total_profit_krw,
            'total_trades': self.total_trades,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': win_rate,
            'avg_profit': avg_profit,
            'max_drawdown': self.max_drawdown,
            'positions': position_details,
            'trading_hours': hours,
            'start_time': self.start_time.strftime('%Y-%m-%d %H:%M:%S') if self.start_time else None
        }
    
    def get_coin_stats(self):
        """코인별 통계 조회"""
        return dict(self.coin_stats)
    
    def get_recent_trades(self, limit=10):
        """최근 거래 내역"""
        return self.trades[-limit:] if len(self.trades) > limit else self.trades
    
    def export_stats(self):
        """통계 내보내기 (JSON)"""
        status = self.get_current_status()
        status['coin_stats'] = self.get_coin_stats()
        status['recent_trades'] = [
            {
                'timestamp': t['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                'coin': t['coin'],
                'buy_price': t['buy_price'],
                'sell_price': t['sell_price'],
                'profit_rate': t['profit_rate'],
                'profit_krw': t['profit_krw'],
                'reason': t['reason']
            }
            for t in self.get_recent_trades(20)
        ]
        return status
    
    def save_positions(self):
        """포지션 스냅샷 저장"""
        try:
            snapshot = {
                'timestamp': datetime.now().isoformat(),
                'positions': {}
            }
            
            for coin, pos in self.positions.items():
                snapshot['positions'][coin] = {
                    'buy_price': pos['buy_price'],
                    'amount': pos['amount'],
                    'original_amount': pos['original_amount'],
                    'timestamp': pos['timestamp'].isoformat(),
                    'highest_price': pos['highest_price'],
                    'uuid': pos.get('uuid')
                }
            
            with open(self.position_file, 'w') as f:
                json.dump(snapshot, f, indent=2)
        except Exception as e:
            print(f"포지션 저장 실패: {e}")
    
    def load_positions(self):
        """포지션 스냅샷 로드"""
        try:
            if not os.path.exists(self.position_file):
                return {}
            
            with open(self.position_file, 'r') as f:
                snapshot = json.load(f)
            
            positions = {}
            for coin, pos in snapshot.get('positions', {}).items():
                positions[coin] = {
                    'buy_price': pos['buy_price'],
                    'amount': pos['amount'],
                    'original_amount': pos['original_amount'],
                    'timestamp': datetime.fromisoformat(pos['timestamp']),
                    'highest_price': pos['highest_price'],
                    'uuid': pos.get('uuid')
                }
            
            return positions
        except Exception as e:
            print(f"포지션 로드 실패: {e}")
            return {}
    
    def _save_trade_to_file(self, trade_record):
        """거래 기록을 일자별 파일에 저장"""
        try:
            today = datetime.now().strftime('%Y%m%d')
            filepath = os.path.join(self.history_dir, f"{today}.json")
            
            # 기존 파일 읽기
            trades = []
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    trades = json.load(f)
            
            # 새 거래 추가
            trades.append(trade_record)
            
            # 파일 저장
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(trades, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"거래 히스토리 저장 실패: {e}")
    
    def load_daily_trades(self, date=None):
        """특정 날짜의 거래 히스토리 로드"""
        try:
            if date is None:
                date = datetime.now().strftime('%Y%m%d')
            elif isinstance(date, datetime):
                date = date.strftime('%Y%m%d')
            
            filepath = os.path.join(self.history_dir, f"{date}.json")
            
            if not os.path.exists(filepath):
                return []
            
            with open(filepath, 'r', encoding='utf-8') as f:
                trades = json.load(f)
            
            # timestamp를 datetime 객체로 변환
            for trade in trades:
                trade['timestamp'] = datetime.fromisoformat(trade['timestamp'])
            
            return trades
        except Exception as e:
            print(f"거래 히스토리 로드 실패: {e}")
            return []
    
    def get_daily_profit(self):
        """일일 손익 계산 (메모리 + 파일 기록 통합)"""
        with self.lock:
            today = datetime.now().date()
            
            # 오늘의 파일 거래 기록
            file_trades = self.load_daily_trades()
            
            # 메모리의 오늘 거래 기록
            memory_trades = [t for t in self.trades if t['timestamp'].date() == today]
            
            # 중복 제거 (timestamp 기준)
            all_trades = {t['timestamp'].isoformat(): t for t in file_trades}
            for t in memory_trades:
                all_trades[t['timestamp'].isoformat()] = t
            
            # 총 손익 계산
            total_profit = sum(t['profit_krw'] for t in all_trades.values())
            
            return total_profit, len(all_trades)

