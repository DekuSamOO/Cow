"""tests/core/test_divergence.py — 頂/底背離偵測單元測試（合成資料，確定性）"""
import numpy as np
import pandas as pd
import pytest

from core.divergence import detect_top_divergence, detect_bottom_divergence, detect_swing_structure


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


# ---------------------------------------------------------------------------
# detect_swing_structure — 結構高低點偵測（合成階梯狀高低點，確定性）
# ---------------------------------------------------------------------------

def _zigzag(points, seg_len=8):
    """把一串轉折值用 linspace 折線相連，中間每個轉折點會形成明確的局部極值。"""
    seg = []
    for i in range(len(points) - 1):
        piece = list(np.linspace(points[i], points[i + 1], seg_len))
        if i > 0:
            piece = piece[1:]
        seg += piece
    return np.array(seg)


def _structure_df(points, seg_len=8, low_offset=5.0):
    """由一串轉折值造出 high/low/close 欄位：high=zigzag(points)，low=high-offset
    （轉折位置與 high 相同，只是整體平移，讓 low 欄位也有對應的局部極值）。"""
    high = _zigzag(points, seg_len)
    low = high - low_offset
    close = (high + low) / 2
    n = len(high)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


def test_swing_structure_hh_hl_bull():
    # 前高80→後高100（HH）、前低60→後低75（HL，低點也墊高）→ 多頭結構延續
    df = _structure_df([50, 80, 60, 100, 75, 85])
    res = detect_swing_structure(df, lookback=len(df), order=4)
    assert res["structure"] == "HH_HL"
    assert res["higher_high"] is True
    assert res["higher_low"] is True
    assert res["last_high"] == pytest.approx(100.0)
    assert res["prior_high"] == pytest.approx(80.0)
    assert res["last_low"] == pytest.approx(70.0)   # 75 - 5
    assert res["prior_low"] == pytest.approx(55.0)  # 60 - 5
    assert res["last_high_pivot_age"] is not None
    assert res["last_low_pivot_age"] is not None


def test_swing_structure_lh_ll_bear():
    # 前高100→後高85（LH）、前低70→後低50（LL）→ 空頭結構延續
    df = _structure_df([90, 100, 70, 85, 50, 60])
    res = detect_swing_structure(df, lookback=len(df), order=4)
    assert res["structure"] == "LH_LL"
    assert res["higher_high"] is False
    assert res["higher_low"] is False


def test_swing_structure_mixed_front_high_not_breached():
    # 前高100→後高90（未過前高，higher_high=False）、前低60→後低75（higher_low=True）
    # → mixed（前高未過但前低墊高，可能是頭部/底部轉折）
    df = _structure_df([50, 100, 60, 90, 75, 85])
    res = detect_swing_structure(df, lookback=len(df), order=4)
    assert res["structure"] == "mixed"
    assert res["higher_high"] is False
    assert res["higher_low"] is True


def test_swing_structure_insufficient_data():
    # df 太短 / 缺欄位 / pivot 不足 → 全部回 None，不 raise
    assert detect_swing_structure(None)["structure"] is None
    assert detect_swing_structure(pd.DataFrame())["structure"] is None
    df_missing_col = pd.DataFrame({"close": np.linspace(50, 60, 30)})
    assert detect_swing_structure(df_missing_col)["structure"] is None
    # 只有單調上升、抓不到 2 個高點/低點 pivot
    n = 30
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    df_monotonic = pd.DataFrame({
        "high": np.linspace(50, 80, n), "low": np.linspace(45, 75, n),
    }, index=idx)
    res = detect_swing_structure(df_monotonic, order=4)
    assert res["structure"] is None
    assert res["last_high"] is None
    assert res["last_high_pivot_age"] is None
