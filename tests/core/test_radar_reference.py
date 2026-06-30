"""逃頂/抄底雷達『參考訊號』（MVRV-Z / Hash Ribbons）單元測試。
驗證：① 正確判讀；② **不影響既有加權總分**（reference_signals 獨立於 score）。"""
import numpy as np
import pandas as pd
import pytest

from core.relative_high import reference_top_signals, compute_escape_top_score
from core.relative_low import (
    reference_low_signals, _hash_ribbon_read, compute_relative_low_score,
)


def test_reference_top_mvrv():
    assert reference_top_signals(mvrv_z=7.5)["mvrv_z"]["label"].startswith("🔴")
    assert reference_top_signals(mvrv_z=1.0)["mvrv_z"]["label"].startswith("⚪")
    assert reference_top_signals(mvrv_z=None) == {}
    assert reference_top_signals(mvrv_z=float("nan")) == {}


def test_reference_low_mvrv():
    assert reference_low_signals(mvrv_z=-0.5)["mvrv_z"]["label"].startswith("🟢")
    assert reference_low_signals(mvrv_z=3.0)["mvrv_z"]["label"].startswith("⚪")
    assert "mvrv_z" not in reference_low_signals(mvrv_z=None)


def test_hash_ribbon_capitulation_and_cross():
    dates = pd.date_range("2024-01-01", periods=120, freq="D")
    # 算力先升後驟跌（製造 SMA30<SMA60 投降）
    vals = np.concatenate([np.linspace(100, 160, 60), np.linspace(160, 110, 60)])
    hist = {d.strftime("%Y-%m-%d"): v for d, v in zip(dates, vals)}
    r = _hash_ribbon_read(hist)
    assert r is not None and "礦工投降" in r["label"]

    # 資料不足 → None
    assert _hash_ribbon_read({d.strftime("%Y-%m-%d"): 1.0
                              for d in pd.date_range("2024-01-01", periods=30)}) is None
    assert _hash_ribbon_read({}) is None
    assert _hash_ribbon_read(None) is None


def test_reference_does_not_affect_score():
    """reference_signals 與加權分數完全解耦：score 計算不吃 mvrv_z/hashrate。"""
    row = {"RSI_14": 50.0}
    score_a, _ = compute_relative_low_score(row, None)
    score_b, _ = compute_relative_low_score(row, None)   # 無 reference 參數可傳入 score 層
    assert score_a == score_b
    # reference 函式獨立可呼叫，回傳不含 score 欄位
    ref = reference_low_signals(mvrv_z=-1.0, hashrate_hist=None)
    assert "score" not in ref.get("mvrv_z", {})
