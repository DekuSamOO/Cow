"""core.risk（ATR 風控框架，純函數）單元測試。watcher／LINE 推播共用同一份 compute_atr_risk。"""
import numpy as np
import pandas as pd

from core.risk import compute_atr_risk, atr_risk_rows


def _df(n=80, atr=5.0, high=120.0, low=80.0):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "high": np.full(n, high), "low": np.full(n, low),
        "close": np.full(n, 100.0), "ATR": np.full(n, atr),
    }, index=idx)


def test_compute_atr_risk_stops_and_levels():
    r = compute_atr_risk(_df(atr=5.0, high=120.0, low=80.0), price=100.0, k=2.0)
    assert r["stop_long"] == 90.0 and r["stop_short"] == 110.0    # 100 ∓ 2×5
    assert r["resistance"] == 120.0
    assert r["reward_risk"] == 2.0                                # (120-100)/(2*5)


def test_compute_atr_risk_support_override_used():
    r = compute_atr_risk(_df(low=80.0), price=100.0, support=95.0)
    assert r["support"] == 95.0        # 傳入支撐（動態地板）優先於近期低


def test_compute_atr_risk_no_reward_when_price_above_high():
    r = compute_atr_risk(_df(high=100.0), price=100.0)
    assert r["reward_risk"] is None    # 現價已在近 60 日高，無多方風報比


def test_compute_atr_risk_insufficient_data_returns_none():
    assert compute_atr_risk(None, 100.0) is None
    assert compute_atr_risk(_df(n=10), 100.0) is None             # < 20 根
    df = _df(); df = df.drop(columns=["ATR"])
    assert compute_atr_risk(df, 100.0) is None                    # 無 ATR 欄


def test_atr_risk_rows_formats_two_lines():
    rows = atr_risk_rows(_df(atr=5.0, high=120.0, low=80.0), price=100.0, k=2.0)
    assert len(rows) == 2
    joined = " ".join(rows)
    assert "ATR(14) $5" in joined
    assert "多↓$90" in joined and "空↑$110" in joined
    assert "1:2.0" in joined


def test_atr_risk_rows_empty_on_insufficient_data():
    assert atr_risk_rows(None, 100.0) == []
