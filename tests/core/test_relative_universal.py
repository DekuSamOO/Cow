"""tests/core/test_relative_universal.py — 通用逃頂/抄底子訊號單元測試（合成資料，零網路）。"""
import numpy as np
import pandas as pd
import pytest

from core.relative_universal import (
    score_volume_price_top, score_volume_price_bottom,
    score_structure_top, score_structure_bottom,
)


# ---------------------------------------------------------------------------
# 量價背離共用 helper
# ---------------------------------------------------------------------------

def _vol_price_df(n=40, vol_short=5, vol_long=20, vol_short_avg=None, vol_long_avg=1_000_000.0,
                   closes=None):
    """建構 volume/close 序列。預設均量序列（無背離基準）；指定 vol_short_avg 時反解中段量，
    使 mean(tail(vol_long)) 精確等於 vol_long_avg、mean(tail(vol_short)) 精確等於 vol_short_avg
    （tail(vol_long) 與 tail(vol_short) 有重疊，不能簡單分段填值）。"""
    if closes is None:
        closes = [100.0] * n
    if vol_short_avg is None:
        volumes = [vol_long_avg] * n
    else:
        mid_count = vol_long - vol_short
        mid_val = ((vol_long_avg * vol_long - vol_short * vol_short_avg) / mid_count
                    if mid_count > 0 else vol_long_avg)
        volumes = [vol_long_avg] * (n - vol_long) + [mid_val] * mid_count + [vol_short_avg] * vol_short
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": closes, "volume": volumes}, index=idx)


def _closes_for_top(n=40, price_short=5):
    """建構「量增+價轉弱」的收盤序列：前一段報酬率為正、近期段報酬率為負（翻黑）。"""
    closes = [100.0] * (n - 2 * price_short)
    # 前一段：緩漲（ret_prior > 0）
    seg_prior = list(np.linspace(closes[-1], closes[-1] * 1.05, price_short + 1))[1:]
    closes += seg_prior
    # 近期段：下跌（ret_now < 0，且 < ret_prior）
    seg_now = list(np.linspace(closes[-1], closes[-1] * 0.95, price_short + 1))[1:]
    closes += seg_now
    return closes


def _closes_for_bottom(n=40, price_short=5):
    """建構「量縮+價守穩」的收盤序列：前一段持平、近期段小漲。"""
    closes = [100.0] * (n - 2 * price_short)
    seg_prior = list(np.linspace(closes[-1], closes[-1], price_short + 1))[1:]
    closes += seg_prior
    seg_now = list(np.linspace(closes[-1], closes[-1] * 1.03, price_short + 1))[1:]
    closes += seg_now
    return closes


def _flat_closes(n=40):
    return [100.0] * n


# ---------------------------------------------------------------------------
# score_volume_price_top
# ---------------------------------------------------------------------------

def test_volume_price_top_high_score_on_volume_up_price_down():
    n = 40
    closes = _closes_for_top(n=n, price_short=5)
    df = _vol_price_df(n=n, vol_short_avg=2_000_000.0, vol_long_avg=1_000_000.0, closes=closes)
    res = score_volume_price_top(df, vol_short=5, vol_long=20, price_short=5)
    assert res["score"] == 15
    assert res["max"] == 15
    assert "🔴" in res["label"]
    assert res["sub"]["vol_ratio"] == pytest.approx(2.0)
    assert res["sub"]["ret_now"] < 0
    assert res["sub"]["ret_now"] < res["sub"]["ret_prior"]


def test_volume_price_top_zero_when_no_divergence():
    n = 40
    df = _vol_price_df(n=n, vol_long_avg=1_000_000.0, closes=_flat_closes(n))
    res = score_volume_price_top(df, vol_short=5, vol_long=20, price_short=5)
    assert res["score"] == 0
    assert res["max"] == 15
    assert "⚪" in res["label"]


def test_volume_price_top_insufficient_data():
    df = _vol_price_df(n=10)   # 小於 vol_long=20
    res = score_volume_price_top(df)
    assert res["score"] == 0
    assert res["max"] == 15
    assert res["label"] == "量價 ⚪ 資料不足"
    assert res["sub"] == {}


# ---------------------------------------------------------------------------
# score_volume_price_bottom
# ---------------------------------------------------------------------------

def test_volume_price_bottom_high_score_on_volume_down_price_stable():
    n = 40
    closes = _closes_for_bottom(n=n, price_short=5)
    df = _vol_price_df(n=n, vol_short_avg=500_000.0, vol_long_avg=1_000_000.0, closes=closes)
    res = score_volume_price_bottom(df, vol_short=5, vol_long=20, price_short=5)
    assert res["score"] == 15
    assert res["max"] == 15
    assert "🟢" in res["label"]
    assert res["sub"]["vol_ratio"] == pytest.approx(0.5)
    assert res["sub"]["ret_now"] > 0


def test_volume_price_bottom_zero_when_no_divergence():
    n = 40
    df = _vol_price_df(n=n, vol_long_avg=1_000_000.0, closes=_flat_closes(n))
    res = score_volume_price_bottom(df, vol_short=5, vol_long=20, price_short=5)
    assert res["score"] == 0
    assert res["max"] == 15
    assert "⚪" in res["label"]


def test_volume_price_bottom_insufficient_data():
    df = _vol_price_df(n=10)
    res = score_volume_price_bottom(df)
    assert res["score"] == 0
    assert res["max"] == 15
    assert res["label"] == "量價 ⚪ 資料不足"
    assert res["sub"] == {}


# ---------------------------------------------------------------------------
# score_structure_top / score_structure_bottom — 複用 detect_swing_structure 合成資料
# ---------------------------------------------------------------------------

def _zigzag(points, seg_len=8):
    seg = []
    for i in range(len(points) - 1):
        piece = list(np.linspace(points[i], points[i + 1], seg_len))
        if i > 0:
            piece = piece[1:]
        seg += piece
    return np.array(seg)


def _structure_df(points, seg_len=8, low_offset=5.0):
    high = _zigzag(points, seg_len)
    low = high - low_offset
    close = (high + low) / 2
    n = len(high)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


def test_structure_top_mixed_front_high_not_breached():
    # 前高100→後高90（未過）、前低60→後低75（墊高）→ mixed, higher_low=True → 逃頂維打滿分
    df = _structure_df([50, 100, 60, 90, 75, 85])
    res = score_structure_top(df, lookback=len(df), order=4)
    assert res["score"] == 10
    assert res["max"] == 10
    assert "前高未過" in res["label"]


def test_structure_bottom_mixed_front_low_not_broken():
    # 同一份 mixed 資料，從抄底視角看＝前低未破（結構轉強）
    df = _structure_df([50, 100, 60, 90, 75, 85])
    res = score_structure_bottom(df, lookback=len(df), order=4)
    assert res["score"] == 10
    assert res["max"] == 10
    assert "前低未破" in res["label"]


def test_structure_top_lh_ll_bear_continuation():
    # 前高100→後高85（LH）、前低70→後低50（LL）→ 空頭結構延續 → 逃頂中等分
    df = _structure_df([90, 100, 70, 85, 50, 60])
    res = score_structure_top(df, lookback=len(df), order=4)
    assert res["score"] == 6
    assert "空頭結構延續" in res["label"]


def test_structure_bottom_hh_hl_bull_continuation():
    # 前高80→後高100（HH）、前低60→後低75（HL）→ 多頭結構延續 → 抄底中等分
    df = _structure_df([50, 80, 60, 100, 75, 85])
    res = score_structure_bottom(df, lookback=len(df), order=4)
    assert res["score"] == 6
    assert "多頭結構延續" in res["label"]


def test_structure_top_zero_when_bull_continuation():
    # 多頭結構延續（HH_HL）對逃頂維而言不是轉弱訊號 → 0 分
    df = _structure_df([50, 80, 60, 100, 75, 85])
    res = score_structure_top(df, lookback=len(df), order=4)
    assert res["score"] == 0
    assert "⚪" in res["label"]


def test_structure_bottom_zero_when_bear_continuation():
    # 空頭結構延續（LH_LL）對抄底維而言不是轉強訊號 → 0 分
    df = _structure_df([90, 100, 70, 85, 50, 60])
    res = score_structure_bottom(df, lookback=len(df), order=4)
    assert res["score"] == 0
    assert "⚪" in res["label"]


def test_structure_insufficient_data():
    res_top = score_structure_top(None)
    res_bot = score_structure_bottom(pd.DataFrame())
    assert res_top["score"] == 0 and res_top["label"] == "結構 ⚪ 資料不足"
    assert res_bot["score"] == 0 and res_bot["label"] == "結構 ⚪ 資料不足"


# ---------------------------------------------------------------------------
# graceful 處理：df=None 或欄位缺失一律 score=0，不 raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn", [score_volume_price_top, score_volume_price_bottom,
                                 score_structure_top, score_structure_bottom])
def test_all_fns_graceful_on_none(fn):
    res = fn(None)
    assert res["score"] == 0


@pytest.mark.parametrize("fn", [score_volume_price_top, score_volume_price_bottom])
def test_volume_price_fns_graceful_on_missing_columns(fn):
    df = pd.DataFrame({"close": [100.0] * 30})   # 缺 volume 欄
    res = fn(df)
    assert res["score"] == 0
    df2 = pd.DataFrame({"volume": [1_000_000.0] * 30})   # 缺 close 欄
    res2 = fn(df2)
    assert res2["score"] == 0


@pytest.mark.parametrize("fn", [score_structure_top, score_structure_bottom])
def test_structure_fns_graceful_on_missing_columns(fn):
    df = pd.DataFrame({"close": [100.0] * 30})   # 缺 high/low 欄
    res = fn(df)
    assert res["score"] == 0
