"""core/radar_replay 回放序列與門檻統計測試（合成資料，不打網路）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pandas as pd

from core.radar_replay import (escape_score_series, low_score_series,
                               threshold_forward_stats, trend_score_series)


def _synthetic_df(n=400) -> pd.DataFrame:
    """帶必要指標欄位的合成日線（緩漲後急跌，製造分數變化）。"""
    idx = pd.date_range("2024-01-01", periods=n)
    close = np.concatenate([np.linspace(50000, 90000, n - 100),
                            np.linspace(90000, 60000, 100)])
    df = pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": 100.0,
    }, index=idx)
    from core.indicators import calculate_technical_indicators, calculate_ahr999
    from core.bear_bottom import calculate_bear_bottom_indicators
    df = calculate_technical_indicators(df)
    df = calculate_ahr999(df)
    df = calculate_bear_bottom_indicators(df)
    return df


def test_score_series_shapes_and_ranges():
    df = _synthetic_df()
    start = 200
    esc = escape_score_series(df, start=start)
    low = low_score_series(df, start=start)
    trd = trend_score_series(df, start=start)
    assert len(esc) == len(df) - start == len(low) == len(trd)
    assert esc.between(0, 100).all() and low.between(0, 100).all()
    assert trd.between(-100, 100).all()
    # 索引對齊原始日線
    assert (esc.index == df.index[start:]).all()


def test_fng_and_funding_injection():
    df = _synthetic_df(300)
    fng = {d.strftime("%Y-%m-%d"): 95.0 for d in df.index}  # 全期極貪
    fund = pd.Series(0.09, index=df.index)                  # 8h 0.09% ≈ 年化 98%（極端過熱）
    esc_hot = escape_score_series(df, fund_daily=fund, fng_map=fng, start=250)
    esc_base = escape_score_series(df, start=250)
    # 注入極端資料後逃頂分必須整段更高（資費 20 + F&G 10）
    assert (esc_hot - esc_base >= 28).all()


def test_threshold_forward_stats_basic():
    idx = pd.date_range("2024-01-01", periods=200)
    # 分數第 50 天跨越 60，價格其後下跌 25% → top 模式命中率 100%
    scores = pd.Series(0.0, index=idx)
    scores.iloc[50:] = 70.0
    close = pd.Series(100.0, index=idx)
    close.iloc[51:] = 75.0
    out = threshold_forward_stats(scores, close, thresholds=(60,), horizon=60, mode="top")
    row = out.iloc[0]
    assert row["事件數"] == 1
    assert row["命中率"] == 1.0
    assert row["中位最大跌幅"] <= -0.18


def test_threshold_forward_stats_cooldown():
    idx = pd.date_range("2024-01-01", periods=200)
    scores = pd.Series(0.0, index=idx)
    # 三次跨越但間隔 < cooldown=30 → 只計第一次
    for i in (50, 60, 70):
        scores.iloc[i] = 70.0
    close = pd.Series(100.0, index=idx)
    out = threshold_forward_stats(scores, close, thresholds=(60,), horizon=30,
                                  mode="top", cooldown=30)
    assert out.iloc[0]["事件數"] == 1
