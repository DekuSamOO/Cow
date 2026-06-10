"""tests/core/test_trend_direction.py — 趨勢方向評分單元測試（合成資料，確定性）"""
import pandas as pd
import pytest

from core.trend_direction import (
    compute_trend_score, trend_meta, compute_trend_direction,
    WEIGHTS_TREND, ADX_NO_TREND, WEAK_TREND_DISCOUNT,
)


def _row(close, sma50, sma200, macd, signal, hist, adx, slope200=None):
    """建構一筆最新日線。slope200 預設依 close-sma200 給合理同向值。"""
    if slope200 is None:
        slope200 = (close - sma200) * 0.02
    return pd.Series({
        "close": close, "SMA_50": sma50, "SMA_200": sma200,
        "MACD": macd, "MACD_Signal": signal, "MACD_Hist": hist,
        "ADX": adx, "SMA_200_Slope": slope200,
    })


def test_weights_sum_to_100():
    assert sum(WEIGHTS_TREND.values()) == 100


def test_strong_bull_high_positive():
    # 完全多頭排列 + 零軸上金叉 + SMA200 升 + 強 ADX → net 應 ≥ 50
    row = _row(close=70000, sma50=64000, sma200=58000,
               macd=800, signal=400, hist=400, adx=42, slope200=2500)
    net, sig = compute_trend_score(row)
    assert net >= 50
    assert "多頭" in trend_meta(net)[0]
    assert sig["ma_structure"]["score"] == WEIGHTS_TREND["ma_structure"]   # 滿 +40
    assert sig["macd"]["score"] == WEIGHTS_TREND["macd"]                   # 滿 +30
    assert sig["adx"]["score"] > 0                                         # 同向加分


def test_strong_bear_high_negative():
    # 完全空頭排列 + 零軸下死叉 + SMA200 降 + 強 ADX → net 應 ≤ -50
    row = _row(close=42000, sma50=48000, sma200=56000,
               macd=-700, signal=-300, hist=-400, adx=38, slope200=-2200)
    net, sig = compute_trend_score(row)
    assert net <= -50
    assert "空頭" in trend_meta(net)[0]
    assert sig["ma_structure"]["score"] == -WEIGHTS_TREND["ma_structure"]
    assert sig["macd"]["score"] == -WEIGHTS_TREND["macd"]
    assert sig["adx"]["score"] < 0


def test_chop_low_adx_near_zero():
    # 均線糾結 + MACD 微幅 + ADX<20（盤整）→ net 落在盤整帶
    row = _row(close=60000, sma50=60100, sma200=60000,
               macd=20, signal=10, hist=10, adx=14, slope200=50)
    net, sig = compute_trend_score(row)
    assert -20 < net < 20
    assert "盤整" in trend_meta(net)[0]
    assert sig["adx"]["score"] == 0                       # 無趨勢 → ADX 不計方向


def test_weak_trend_discount_applied():
    # 同樣多頭結構，ADX 低於門檻 → 方向分被打折，net 明顯低於高 ADX 版
    bull = dict(close=70000, sma50=64000, sma200=58000,
                macd=800, signal=400, hist=400, slope200=2500)
    strong = compute_trend_score(_row(adx=42, **bull))[0]
    weak = compute_trend_score(_row(adx=15, **bull))[0]
    assert weak < strong
    # 方向三維(40+30+10=80；未傳 df，斜率僅 SMA200 子分上限 10)×折扣，ADX 維度為 0
    assert weak == pytest.approx(round(80 * WEAK_TREND_DISCOUNT), abs=1)


def test_signed_score_bounds():
    # 任意極端輸入，net 不得超出 [-100, 100]
    row = _row(close=999999, sma50=1, sma200=1,
               macd=99999, signal=-99999, hist=99999, adx=99, slope200=999999)
    net, _ = compute_trend_score(row)
    assert -100 <= net <= 100


def test_missing_data_graceful():
    # 全缺：不崩、net≈0、各維 score 0
    row = pd.Series({"close": float("nan")})
    net, sig = compute_trend_score(row)
    assert -100 <= net <= 100
    assert sig["ma_structure"]["score"] == 0
    assert sig["macd"]["score"] == 0


def test_compute_trend_direction_shape():
    row = _row(close=70000, sma50=64000, sma200=58000,
               macd=800, signal=400, hist=400, adx=42, slope200=2500)
    out = compute_trend_direction(row)
    assert set(out) == {"trend_score", "trend_level", "trend_color",
                        "trend_action", "trend_signals"}
    assert isinstance(out["trend_score"], int)
    assert "多頭" in out["trend_level"]
