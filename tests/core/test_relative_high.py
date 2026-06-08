"""tests/core/test_relative_high.py — 相對高點評分單元測試（合成資料，確定性）"""
import math
import numpy as np
import pandas as pd
import pytest

from core.relative_high import (
    annualize_funding, compute_escape_top_score, escape_top_meta,
    compute_cycle_top_estimates, FUNDING_ANN_RED, WEIGHTS, UNFITTED_DIMS,
)


def test_annualize_funding_matches_note():
    # 來源筆記：0.083%/8h ≈ 年化 90%；0.0457%/8h ≈ 年化 50%
    assert annualize_funding(0.083) == pytest.approx(90.9, abs=0.5)
    assert annualize_funding(0.0457) == pytest.approx(50.0, abs=0.5)
    assert annualize_funding(None) is None
    assert annualize_funding(float("nan")) is None


def _row(rsi=50.0):
    return pd.Series({"RSI_14": rsi, "close": 60000.0})


def test_extreme_inputs_high_score():
    row = _row(rsi=82)
    score, sig = compute_escape_top_score(
        row, None,
        funding_8h=0.10,                                   # 年化 >90% → 滿分
        oi_stats={"percentile": 97, "is_near_high": True, "n": 90},
        etf_summary={"n": 600, "consecutive_outflow_days": 11},
        sopr=1.09, fng=92,
        btc_d_trend={"change_pp": -1.5, "is_falling": True},
        macro={"cpi_hot": True, "jobs_strong": True, "event_within_days": 1},
    )
    assert score >= 75
    level, color, action = escape_top_meta(score)
    assert "逃頂" in level
    # 合約過熱維度應接近滿分
    assert sig["derivatives"]["score"] >= 25


def test_calm_inputs_low_score():
    row = _row(rsi=45)
    score, sig = compute_escape_top_score(
        row, None,
        funding_8h=0.005,                                  # 年化 ~5%
        oi_stats={"percentile": 40, "n": 90},
        etf_summary={"n": 600, "consecutive_outflow_days": 0},
        sopr=1.00, fng=40,
        btc_d_trend={"change_pp": 0.2, "is_falling": False},
        macro=None,
    )
    assert score <= 20
    assert escape_top_meta(score)[0].endswith("無過熱") or "中性" in escape_top_meta(score)[0]


def test_missing_data_graceful():
    # 全部資料缺：不應崩、分數低、各維有「累積中/無資料」標籤
    row = _row(rsi=float("nan"))
    score, sig = compute_escape_top_score(row, None)
    assert score == 0
    for dim in WEIGHTS:
        assert dim in sig
        assert sig[dim]["score"] == 0


def test_dimension_max_caps():
    # 每維分數不得超過其權重上限
    row = _row(rsi=85)
    _, sig = compute_escape_top_score(
        row, None, funding_8h=0.2,
        oi_stats={"percentile": 99, "is_near_high": True, "n": 90},
        etf_summary={"n": 9, "consecutive_outflow_days": 99},
        sopr=2.0, fng=99,
        btc_d_trend={"change_pp": -5, "is_falling": True},
        macro={"cpi_hot": True, "pce_hot": True, "jobs_strong": True, "event_within_days": 0})
    for dim, s in sig.items():
        assert s["score"] <= WEIGHTS[dim], f"{dim} 超過上限"


def test_onchain_is_unfitted():
    assert "onchain" in UNFITTED_DIMS


def test_cycle_top_estimates_sorted():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({
        "close": [60000, 61000, 62000],
        "SMA_350x2": [np.nan, np.nan, 120000.0],
        "SMA_730":   [np.nan, np.nan, 40000.0],     # Mayer 頂 = 96000
        "PowerLaw_Support": [np.nan, np.nan, 50000.0],  # 上界 ≈ 141000
    }, index=idx)
    est = compute_cycle_top_estimates(62000.0, df)
    vals = [e["value"] for e in est]
    assert vals == sorted(vals, reverse=True)         # 由高到低
    labels = [e["label"] for e in est]
    assert "Mayer 頂" in labels and "Pi Cycle 頂訊號線" in labels
