"""BTC_WATCH._atr_risk_rows 單元測試（ATR 風控框架，純函數）。"""
import numpy as np
import pandas as pd

from BTC_WATCH import _atr_risk_rows


def _df(n=80, atr=5.0, high=120.0, low=80.0):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "high": np.full(n, high), "low": np.full(n, low),
        "close": np.full(n, 100.0), "ATR": np.full(n, atr),
    }, index=idx)


def test_atr_stops_and_levels():
    rows = _atr_risk_rows(_df(atr=5.0, high=120.0, low=80.0), price=100.0, k=2.0)
    assert len(rows) == 2
    joined = " ".join(rows)
    assert "ATR(14) $5" in joined
    assert "多 $90" in joined and "空 $110" in joined     # 100 ∓ 2×5
    assert "壓力" in joined and "$120" in joined
    # price 100 < 前高 120 → 有多方風報比（reward 20 / risk 10 = 1:2.0）
    assert "1:2.0" in joined


def test_support_override_used():
    rows = _atr_risk_rows(_df(low=80.0), price=100.0, support=95.0)
    assert "$95" in rows[1]        # 傳入支撐（動態地板）優先於近期低


def test_no_reward_when_price_above_high():
    # 現價高於近 60 日高 → 無多方風報比欄
    df = _df(high=100.0)
    rows = _atr_risk_rows(df, price=100.0)
    assert "風報 1:" not in " ".join(rows)


def test_insufficient_data_returns_empty():
    assert _atr_risk_rows(None, 100.0) == []
    assert _atr_risk_rows(_df(n=10), 100.0) == []            # < 20 根
    df = _df(); df = df.drop(columns=["ATR"])
    assert _atr_risk_rows(df, 100.0) == []                    # 無 ATR 欄
