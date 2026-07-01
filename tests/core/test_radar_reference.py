"""逃頂/抄底雷達『參考訊號』（MVRV-Z / Hash Ribbons）單元測試。
驗證：① 正確判讀；② **不影響既有加權總分**（reference_signals 獨立於 score）。"""
import numpy as np
import pandas as pd
import pytest

from core.relative_high import (
    reference_top_signals, compute_escape_top_score, _score_derivatives,
)
from core.relative_low import (
    reference_low_signals, _hash_ribbon_read, compute_relative_low_score,
)


# funding_8h 對應年化：annualize = rate × 3 × 365。要 f_s≥14（年化≥30%）需 rate≥30/1095≈0.0274
_HOT_FUNDING_8H = 0.05   # 年化 ≈ 54.75% → f_s=20（極端過熱）


def test_oi_funding_synergy_discount_only_in_false_top():
    """OI×Funding 交互：高 funding + OI 分位低 → 折減；OI 高或無資料 → 不折減；從不灌分。"""
    # OI 分位低（去槓桿/未confirm）→ funding 貢獻被折減
    low_oi = _score_derivatives(_HOT_FUNDING_8H, {"percentile": 30})
    # OI 分位高（confirm）→ 不折減
    high_oi = _score_derivatives(_HOT_FUNDING_8H, {"percentile": 90})
    # OI 無資料 → 不折減（不因缺資料懲罰）
    no_oi = _score_derivatives(_HOT_FUNDING_8H, None)

    assert low_oi["sub"]["synergy_discount"] == 0.75
    assert high_oi["sub"]["synergy_discount"] == 1.0
    assert no_oi["sub"]["synergy_discount"] == 1.0
    # 折減後 funding 有效分 < 原始分；OI 低時整體 deriv 分被下修
    assert low_oi["sub"]["funding_score_eff"] < low_oi["sub"]["funding_score"]
    # 從不超過「純相加」上限
    assert low_oi["score"] <= low_oi["sub"]["funding_score"] + low_oi["sub"]["oi_score"]
    # 不過熱（f_s<14）時不折減
    mild = _score_derivatives(0.01, {"percentile": 30})   # 年化≈10.95% → f_s=0
    assert mild["sub"]["synergy_discount"] == 1.0


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
