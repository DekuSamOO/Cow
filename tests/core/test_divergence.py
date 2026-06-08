"""tests/core/test_divergence.py — 頂/底背離偵測單元測試（合成資料，確定性）"""
import numpy as np
import pandas as pd
import pytest

from core.divergence import detect_top_divergence, detect_bottom_divergence


def _make_df(prices, rsi, macd=None):
    n = len(prices)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "close": prices, "high": prices, "low": prices,
        "RSI_14": rsi,
    }, index=idx)
    if macd is not None:
        df["MACD"] = macd
    return df


def _double_peak(p1, p2, base=50.0, order_pad=8):
    """造兩個明顯高峰：peak1 在中段、peak2 在尾段（peak2 較高/較低由參數定）。"""
    seg = []
    seg += list(np.linspace(base, p1, order_pad))          # 升到 peak1
    seg += list(np.linspace(p1, base, order_pad))[1:]      # 回落
    seg += list(np.linspace(base, p2, order_pad))[1:]      # 升到 peak2
    seg += list(np.linspace(p2, base * 0.9, order_pad))[1:]  # 回落（讓 peak2 成樞紐）
    return np.array(seg)


def test_top_divergence_detected():
    # 價格：peak1=100、peak2=110（HH）；RSI：peak1=75、peak2=65（LH）→ 頂背離
    price = _double_peak(100, 110)
    # RSI 與價格同形但 peak2 較低
    rsi = _double_peak(75, 65, base=45)
    df = _make_df(price, rsi)
    res = detect_top_divergence(df, lookback=len(df), order=4, recent_bars=30)
    assert res["has_divergence"] is True
    assert res["price_change_pct"] > 0      # 價格 HH
    assert res["indicator_change"] < 0      # 指標 LH
    assert 0 < res["strength"] <= 1.0


def test_no_divergence_when_momentum_confirms():
    # 價格 HH 且 RSI 也 HH（動能確認）→ 無頂背離
    price = _double_peak(100, 110)
    rsi = _double_peak(60, 72, base=45)     # peak2 RSI 較高
    df = _make_df(price, rsi)
    res = detect_top_divergence(df, lookback=len(df), order=4, recent_bars=30)
    assert res["has_divergence"] is False


def test_bottom_divergence_detected():
    # 價格 LL、RSI HL → 底背離（造兩個谷）
    trough = -_double_peak(100, 110)        # 兩個低谷（peak2 更低 = LL）
    rsi = _double_peak(28, 38, base=50)     # RSI peak2 較高 = HL
    df = _make_df(trough + 200, rsi)        # 平移成正價
    res = detect_bottom_divergence(df, lookback=len(df), order=4, recent_bars=30)
    assert res["has_divergence"] is True
    assert res["indicator_change"] > 0      # 指標 HL


def test_empty_or_missing_column():
    df = pd.DataFrame()
    assert detect_top_divergence(df)["has_divergence"] is False
    df2 = _make_df(np.linspace(50, 60, 30), np.linspace(50, 60, 30))
    # 指標欄不存在時 graceful
    assert detect_top_divergence(df2, indicator="NOT_A_COL")["has_divergence"] is False


def test_stale_pivot_not_recent():
    # 背離存在但最後樞紐距今很遠（>recent_bars）→ 視為過期，不承認
    price = np.concatenate([_double_peak(100, 110), np.full(40, 45.0)])
    rsi = np.concatenate([_double_peak(75, 65, base=45), np.full(40, 45.0)])
    df = _make_df(price, rsi)
    res = detect_top_divergence(df, lookback=len(df), order=4, recent_bars=10)
    assert res["has_divergence"] is False
