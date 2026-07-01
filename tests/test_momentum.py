"""core/momentum.time_series_momentum 單元測試。"""
import numpy as np
import pandas as pd

from core.momentum import time_series_momentum, momentum_ref_rows


def _df(prices):
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="D")
    return pd.DataFrame({"close": prices}, index=idx)


def test_uptrend_all_positive():
    df = _df(np.linspace(100, 300, 400))          # 單調上漲 → 3/6/12M 皆正
    m = time_series_momentum(df)
    assert m["n"] == 3 and m["n_pos"] == 3 and m["stance"] == "up"
    assert momentum_ref_rows(df)[0].count("+") >= 3


def test_downtrend_all_negative():
    df = _df(np.linspace(300, 100, 400))
    m = time_series_momentum(df)
    assert m["n_pos"] == 0 and m["stance"] == "down"


def test_mixed_stance():
    # 近 90 日漲、但 365 日仍為負（先跌深再反彈）
    prices = np.concatenate([np.linspace(300, 100, 300), np.linspace(100, 160, 100)])
    m = time_series_momentum(_df(prices))
    assert m["stance"] == "mixed" and 0 < m["n_pos"] < m["n"]


def test_insufficient_data():
    m = time_series_momentum(_df(np.linspace(100, 110, 50)))   # < 90 日
    assert m["rets"] == {} and m["stance"] is None
    assert momentum_ref_rows(_df(np.linspace(100, 110, 50))) == []
