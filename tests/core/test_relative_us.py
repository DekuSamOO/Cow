"""core/relative_high_us + relative_low_us 美股評分純函數測試（固定輸入，零網路）。"""
import numpy as np
import pandas as pd

from core.relative_high_us import compute_relative_high_us, relative_high_us_meta, WEIGHTS_HIGH_US
from core.relative_low_us import compute_relative_low_us, relative_low_us_meta, WEIGHTS_LOW_US


def _flat_df(n=30, price=100.0, volume=1_000_000):
    df = pd.DataFrame({"close": [price] * n, "high": [price] * n, "low": [price] * n,
                       "volume": [volume] * n})
    row = pd.Series({"RSI_14": 50, "close": price})
    return row, df


def test_weights_sum_to_100():
    assert sum(WEIGHTS_HIGH_US.values()) == 100
    assert sum(WEIGHTS_LOW_US.values()) == 100


def test_high_all_neutral_gives_zero():
    row, df = _flat_df()
    score, sig = compute_relative_high_us(row, df)
    assert score == 0
    assert all(s["score"] == 0 for s in sig.values())


def test_high_overbought_rsi_gives_positive_score():
    row, df = _flat_df()
    row["RSI_14"] = 85   # 極度超買 → technical 維度加分
    score, sig = compute_relative_high_us(row, df)
    assert sig["technical"]["score"] > 0
    assert score > 0


def test_low_all_neutral_gives_zero():
    row, df = _flat_df()
    score, sig = compute_relative_low_us(row, df)
    assert score == 0


def test_low_oversold_rsi_gives_positive_score():
    row, df = _flat_df()
    row["RSI_14"] = 15   # 極度超賣 → technical 維度加分
    score, sig = compute_relative_low_us(row, df)
    assert sig["technical"]["score"] > 0
    assert score > 0


def test_high_vol_price_and_structure_rescaled_to_declared_max():
    """量增價縮＋量能放大 → vol_price 分數按比例縮放到 WEIGHTS_HIGH_US 宣告的 max（非原始 15）。"""
    n = 60
    close = [100.0] * (n - 10) + list(np.linspace(100, 90, 10))   # 尾段下滑
    vol = [1_000_000] * (n - 5) + [3_000_000] * 5                  # 尾段爆量
    df = pd.DataFrame({"close": close, "high": close, "low": close, "volume": vol})
    row = pd.Series({"RSI_14": 50, "close": close[-1]})
    score, sig = compute_relative_high_us(row, df)
    assert sig["vol_price"]["max"] == WEIGHTS_HIGH_US["vol_price"]
    assert sig["structure"]["max"] == WEIGHTS_HIGH_US["structure"]
    assert sig["vol_price"]["score"] <= WEIGHTS_HIGH_US["vol_price"]


def test_score_clamped_0_100():
    row, df = _flat_df()
    row["RSI_14"] = 85
    score, _ = compute_relative_high_us(row, df)
    assert 0 <= score <= 100
    row["RSI_14"] = 15
    score, _ = compute_relative_low_us(row, df)
    assert 0 <= score <= 100


def test_meta_levels_monotonic_labels():
    assert "逃頂" in relative_high_us_meta(70)[0] or "過熱" in relative_high_us_meta(70)[0]
    assert "無過熱" in relative_high_us_meta(5)[0]
    assert "低估" in relative_low_us_meta(70)[0]
    assert "無底部" in relative_low_us_meta(5)[0]
