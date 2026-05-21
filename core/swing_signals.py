import numpy as np
import pandas as pd
from typing import Optional

def compute_entry_mask(
    df: pd.DataFrame,
    entry_dist_min_pct: float = 0.0,
    entry_dist_max_pct: Optional[float] = None,
    rsi_min: int = 50,
    adx_min: int = 20
) -> pd.Series:
    """
    Antigravity v4.1 波段進場條件 (5合1 過濾)
    返回 Boolean Series（當日訊號，不做 shift）
    
    條件:
    1. 價格 > SMA200 (多頭趨勢)
    2. RSI > rsi_min (動能偏多)
    3. 距離 EMA20 介於 dist_min 與 dist_max 之間
    4. MACD > Signal (動能確認)
    5. ADX > adx_min (趨勢強度足夠)
    """
    if df.empty:
        return pd.Series(dtype=bool)

    close = df['close']
    ema_20 = df.get('EMA_20', close)
    ema_safe = ema_20.replace(0, np.nan).fillna(close)
    dist_pct = (close / ema_safe - 1) * 100

    sma_200 = df.get('SMA_200', pd.Series(0, index=df.index)).fillna(0)
    rsi_14 = df.get('RSI_14', pd.Series(0, index=df.index)).fillna(0)
    
    bull_trend = (close > sma_200) & (rsi_14 > rsi_min)

    if entry_dist_max_pct is not None:
        dist_ok = (dist_pct >= entry_dist_min_pct) & (dist_pct <= entry_dist_max_pct)
    else:
        dist_ok = dist_pct >= entry_dist_min_pct

    if 'MACD_12_26_9' in df.columns and 'MACDs_12_26_9' in df.columns:
        macd_bull = (df['MACD_12_26_9'] > df['MACDs_12_26_9']).fillna(False)
    else:
        macd_bull = pd.Series(True, index=df.index)

    if 'ADX' in df.columns:
        adx_trending = (df['ADX'] > adx_min).fillna(False)
    else:
        adx_trending = pd.Series(True, index=df.index)

    is_entry = bull_trend & dist_ok & macd_bull & adx_trending
    return is_entry


def compute_exit_mask(
    df: pd.DataFrame,
    exit_ma: str = "SMA_50"
) -> pd.Series:
    """
    Antigravity 波段出場防守線條件
    返回 Boolean Series（當日訊號，不做 shift）
    """
    if df.empty:
        return pd.Series(dtype=bool)

    close = df['close']
    if exit_ma in df.columns:
        is_exit = close < df[exit_ma].fillna(0)
    else:
        ema_20 = df.get('EMA_20', close)
        ema_safe = ema_20.replace(0, np.nan).fillna(close)
        is_exit = close < ema_safe

    return is_exit
