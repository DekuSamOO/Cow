import pytest
import pandas as pd
import numpy as np
from core.swing_signals import compute_entry_mask, compute_exit_mask

def test_swing_signals_parity():
    # 產生假資料
    dates = pd.date_range('2023-01-01', periods=10, freq='D')
    
    # 刻意設計的價格與指標，讓某一天剛好符合進場條件
    # 進場條件: close > SMA_200, RSI > 50, dist_pct >= 0, MACD > Signal, ADX > 20
    
    df = pd.DataFrame({
        'close': [100, 105, 110, 108, 115, 120, 118, 125, 130, 135],
        'EMA_20': [90, 92, 95, 98, 100, 105, 110, 115, 120, 125],
        'SMA_200': [80, 80, 80, 80, 80, 80, 80, 80, 80, 80],
        'RSI_14': [40, 45, 55, 48, 60, 65, 55, 70, 75, 80],
        'MACD_12_26_9': [1, 2, 3, 2, 4, 5, 4, 6, 7, 8],
        'MACDs_12_26_9': [0, 1, 2, 3, 3, 4, 5, 5, 6, 7],
        'ADX': [15, 18, 22, 19, 25, 28, 26, 30, 32, 35],
        'SMA_50': [100, 100, 100, 110, 110, 110, 120, 120, 120, 120]
    }, index=dates)

    # 計算 Entry Mask
    entry_mask = compute_entry_mask(df, entry_dist_min_pct=0.0, rsi_min=50, adx_min=20)
    
    # 驗證第 3 天 (110)
    # close=110 > SMA_200=80 (True)
    # RSI=55 > 50 (True)
    # EMA=95, close=110, dist > 0 (True)
    # MACD=3 > MACDs=2 (True)
    # ADX=22 > 20 (True)
    assert entry_mask.iloc[2] == True, "第 3 天應該觸發進場"
    
    # 驗證第 4 天 (108)
    # RSI=48 < 50 (False)
    assert entry_mask.iloc[3] == False, "第 4 天 RSI 不足不該進場"
    
    # 驗證第 7 天 (118)
    # MACD=4 < MACDs=5 (False)
    assert entry_mask.iloc[6] == False, "第 7 天 MACD 死亡交叉不該進場"

    # 驗證第 5 天 (115)
    # MACD=4 > 3, ADX=25>20, RSI=60>50, close>SMA, close>EMA
    assert entry_mask.iloc[4] == True, "第 5 天應該觸發進場"

    # 驗證 Exit Mask (防守線 SMA_50)
    exit_mask = compute_exit_mask(df, exit_ma='SMA_50')
    
    # 第 1 天: close=100 < SMA_50=100 (False)
    assert exit_mask.iloc[0] == False
    # 第 4 天: close=108 < SMA_50=110 (True)
    assert exit_mask.iloc[3] == True
    # 第 7 天: close=118 < SMA_50=120 (True)
    assert exit_mask.iloc[6] == True
    # 第 9 天: close=130 < SMA_50=120 (False)
    assert exit_mask.iloc[8] == False

