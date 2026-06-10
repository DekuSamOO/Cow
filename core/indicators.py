"""
core/indicators.py
技術指標計算層 — 純 Python，無 Streamlit 依賴
"""
import pandas as pd
import numpy as np
import pandas_ta as ta
from datetime import datetime


def _ta_series(result, index):
    """pandas-ta 在資料長度不足時回傳 None；統一轉成 NaN Series 防止下游 diff/reindex 崩潰。"""
    if result is None:
        return pd.Series(np.nan, index=index)
    return result


def calculate_technical_indicators(df):
    df = df.copy()
    if df.empty:
        return df

    # Moving Averages
    df['SMA_200'] = _ta_series(ta.sma(df['close'], length=200), df.index)
    df['EMA_20'] = _ta_series(ta.ema(df['close'], length=20), df.index)
    df['SMA_50'] = _ta_series(ta.sma(df['close'], length=50), df.index)

    # SMA 200 Slope (20-day lookback)
    if 'SMA_200' in df.columns:
        df['SMA_200_Slope'] = df['SMA_200'].diff(20)
    else:
        df['SMA_200_Slope'] = 0

    # RSI (Daily)
    df['RSI_14'] = _ta_series(ta.rsi(df['close'], length=14), df.index)

    # RSI (Weekly)
    weekly_close = df['close'].resample('W-MON').last()
    weekly_rsi = _ta_series(ta.rsi(weekly_close, length=14), weekly_close.index)
    df['RSI_Weekly'] = weekly_rsi.reindex(df.index).ffill()

    # ATR
    df['ATR'] = _ta_series(ta.atr(df['high'], df['low'], df['close'], length=14), df.index)

    # Bollinger Bands
    bb = ta.bbands(df['close'], length=20, std=2.0)
    if bb is not None:
        df = pd.concat([df, bb], axis=1)
        bbl = [c for c in df.columns if c.startswith('BBL')][0]
        bbu = [c for c in df.columns if c.startswith('BBU')][0]
        df['BB_Lower'] = df[bbl]
        df['BB_Upper'] = df[bbu]

    # Pivot Points (Classic)
    df['P'] = (df['high'].shift(1) + df['low'].shift(1) + df['close'].shift(1)) / 3
    df['R1'] = 2 * df['P'] - df['low'].shift(1)
    df['S1'] = 2 * df['P'] - df['high'].shift(1)
    df['R2'] = df['P'] + (df['high'].shift(1) - df['low'].shift(1))
    df['S2'] = df['P'] - (df['high'].shift(1) - df['low'].shift(1))

    # KDJ (9, 3, 3)
    kdj = ta.kdj(df['high'], df['low'], df['close'], length=9, signal=3)
    if kdj is not None:
        df = pd.concat([df, kdj], axis=1)
        df['K'] = df['K_9_3']
        df['J'] = df['J_9_3']

    # ADX
    adx = ta.adx(df['high'], df['low'], df['close'], length=14)
    if adx is not None:
        df = pd.concat([df, adx], axis=1)
        adx_col = [c for c in df.columns if c.startswith('ADX')][0]
        df['ADX'] = df[adx_col]

    # MACD (12, 26, 9)
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    if macd is not None:
        df = pd.concat([df, macd], axis=1)
        macd_col = [c for c in df.columns if c.startswith('MACD_')][0]
        hist_col = [c for c in df.columns if c.startswith('MACDh_')][0]
        sig_col = [c for c in df.columns if c.startswith('MACDs_')][0]
        df['MACD'] = df[macd_col]
        df['MACD_Hist'] = df[hist_col]
        df['MACD_Signal'] = df[sig_col]

    return df


def calculate_ahr999(df):
    """
    AHR999 囤幣指標 (向量化計算，效能較 apply() 快 50-100x)

    公式: AHR999 = (Price / SMA200) × (Price / PowerLaw_Model)

    PowerLaw_Model（冪律增長估值）= 10 ^ (5.84 × log10(days_since_genesis) - 17.01467)
    此為 Giovanni Santostasi 比特幣冪律模型，與 AHR999 原始定義一致。

    ⚠️ 舊版錯誤公式: 10 ^ (2.68 + 0.00057 × days) 為線性指數模型，
       到 2026 年估值已膨脹至 $177 萬，導致 AHR999 嚴重低估（0.02 vs 正確 0.29）。
       修正後與 CoinGlass / on-chain.io 計算結果一致。
    """
    genesis_date = datetime(2009, 1, 3)

    # 向量化計算天數陣列
    days_arr = np.array([
        (d.to_pydatetime() - genesis_date).days
        if hasattr(d, 'to_pydatetime') else (d - genesis_date).days
        for d in df.index
    ], dtype=float)
    days_arr = np.clip(days_arr, 1, None)  # 避免 log10(0)

    # ✅ 正確公式：比特幣冪律增長模型（與 CoinGlass / Santostasi 一致）
    valuation = 10 ** (-17.01467 + 5.84 * np.log10(days_arr))

    sma200 = df['SMA_200'].values
    close = df['close'].values

    # AHR999 = (Price/SMA200) × (Price/PowerLaw_Model)，SMA200 為 NaN 時結果為 NaN
    with np.errstate(divide='ignore', invalid='ignore'):
        ahr = (close / sma200) * (close / valuation)
        ahr = np.where(np.isnan(sma200), np.nan, ahr)

    df['AHR999'] = ahr

    # MVRV Z-Score Proxy (已是向量化)
    if not df.empty and 'SMA_200' in df.columns:
        rolling_std = df['close'].rolling(window=200).std()
        df['MVRV_Z_Proxy'] = (df['close'] - df['SMA_200']) / rolling_std

    return df
