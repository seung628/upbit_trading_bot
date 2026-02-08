"""
코인 선정 모듈 - 단타 거래에 적합한 코인 선정
"""

import pyupbit
import pandas as pd
import numpy as np
from datetime import datetime


class CoinSelector:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        
        self.min_volume = config['coin_selection']['min_volume_krw']
        self.min_volatility = config['coin_selection']['min_volatility']
        self.max_volatility = config['coin_selection']['max_volatility']
        self.excluded_coins = config['coin_selection'].get('excluded_coins', [])
    
    def get_top_coins(self, max_coins=3):
        """단타 거래에 적합한 코인 선정"""
        
        self.logger.info("🔍 거래 적합 코인 분석 시작...")
        
        try:
            tickers = pyupbit.get_tickers(fiat="KRW")
            coin_data = []
            
            for ticker in tickers:
                try:
                    # 제외 코인 필터링
                    coin_symbol = ticker.replace("KRW-", "")
                    if coin_symbol in self.excluded_coins:
                        continue
                    
                    # 일봉 데이터
                    df_day = pyupbit.get_ohlcv(ticker, interval="day", count=1)
                    if df_day is None or len(df_day) == 0:
                        continue
                    
                    # 분봉 데이터
                    df_min = pyupbit.get_ohlcv(ticker, interval="minute1", count=200)
                    if df_min is None or len(df_min) < 100:
                        continue
                    
                    # 기본 정보
                    current_price = df_day['close'].iloc[-1]
                    volume_krw = df_day['value'].iloc[-1]
                    
                    # 변동성 계산
                    high_24h = df_day['high'].iloc[-1]
                    low_24h = df_day['low'].iloc[-1]
                    volatility = ((high_24h - low_24h) / low_24h) * 100
                    
                    # 최소 조건 필터링
                    if volume_krw < self.min_volume:
                        continue
                    if volatility < self.min_volatility or volatility > self.max_volatility:
                        continue
                    
                    # 거래량 추세 (증가하는지 확인)
                    recent_volume = df_min['volume'].tail(60).mean()
                    older_volume = df_min['volume'].iloc[-180:-60].mean()
                    volume_trend = (recent_volume / older_volume) if older_volume > 0 else 1
                    
                    # 볼린저밴드 폭 (변동성 지표)
                    df_min['ma20'] = df_min['close'].rolling(20).mean()
                    df_min['std20'] = df_min['close'].rolling(20).std()
                    df_min['bb_width'] = (df_min['std20'] / df_min['ma20']) * 100
                    bb_width = df_min['bb_width'].tail(100).mean()
                    
                    # RSI 계산 (과매도 상태 확인)
                    delta = df_min['close'].diff()
                    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs = gain / loss
                    rsi = 100 - (100 / (1 + rs))
                    current_rsi = rsi.iloc[-1]
                    
                    # 점수 계산
                    score = self._calculate_score(
                        volume_krw, volatility, volume_trend, bb_width, current_rsi
                    )
                    
                    coin_data.append({
                        'ticker': ticker,
                        'name': ticker.replace("KRW-", ""),
                        'price': current_price,
                        'volume_krw': volume_krw,
                        'volatility': volatility,
                        'volume_trend': volume_trend,
                        'bb_width': bb_width,
                        'rsi': current_rsi,
                        'score': score
                    })
                    
                except Exception as e:
                    continue
            
            if not coin_data:
                self.logger.warning("⚠️  조건에 맞는 코인이 없습니다.")
                return []
            
            # 점수 순으로 정렬
            df = pd.DataFrame(coin_data)
            df = df.sort_values('score', ascending=False)
            
            # 상위 코인 선정
            top_coins = df.head(max_coins)
            
            # 결과 출력
            self.logger.info("="*80)
            self.logger.info("🏆 선정된 거래 코인")
            if self.excluded_coins:
                self.logger.info(f"   (제외 코인: {', '.join(self.excluded_coins)})")
            self.logger.info("="*80)
            
            for idx, row in top_coins.iterrows():
                self.logger.info(
                    f"  [{top_coins.index.get_loc(idx)+1}] {row['name']} | "
                    f"가격: {row['price']:,.0f}원 | "
                    f"거래량: {row['volume_krw']/100000000:.0f}억 | "
                    f"변동성: {row['volatility']:.2f}% | "
                    f"RSI: {row['rsi']:.1f} | "
                    f"점수: {row['score']:.1f}"
                )
            
            self.logger.info("="*80)
            
            return top_coins['ticker'].tolist()
            
        except Exception as e:
            self.logger.log_error("코인 선정 중 오류 발생", e)
            return []
    
    def _calculate_score(self, volume_krw, volatility, volume_trend, bb_width, rsi):
        """코인 점수 계산"""
        
        # 거래량 점수 (로그 스케일)
        volume_score = np.log10(volume_krw / 1_000_000_000) * 10
        
        # 변동성 점수 (적당한 변동성 선호)
        if 4 <= volatility <= 8:
            volatility_score = 30
        elif 3 <= volatility < 4:
            volatility_score = 20
        elif 8 < volatility <= 10:
            volatility_score = 20
        else:
            volatility_score = 10
        
        # 거래량 추세 점수
        if volume_trend > 1.2:
            trend_score = 20
        elif volume_trend > 1.0:
            trend_score = 10
        else:
            trend_score = 5
        
        # BB 폭 점수
        if 1 <= bb_width <= 3:
            bb_score = 20
        elif 0.5 <= bb_width < 1:
            bb_score = 10
        else:
            bb_score = 5
        
        # RSI 점수 (과매도 선호)
        if rsi < 40:
            rsi_score = 20
        elif rsi < 50:
            rsi_score = 10
        else:
            rsi_score = 5
        
        total_score = volume_score + volatility_score + trend_score + bb_score + rsi_score
        
        return total_score
