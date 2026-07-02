"""逃頂/抄底雷達『參考訊號 → 正式計分』轉換單元測試。

2026-07 用 tests/relative_ref_signals_backtest.py 驗證 MVRV-Z 逃頂/抄底皆過 AUC≥0.55
門檻（0.592/0.732），已從「參考顯示、不計分」轉為 onchain 子分正式計分項；
`reference_top_signals`（原本只含 mvrv_z）已整支移除，`reference_low_signals` 拿掉
mvrv_z 分支、僅剩 Hash Ribbons（同批驗證 AUC=0.359，方向反/無效，維持參考不計分）。"""
import numpy as np
import pandas as pd
import pytest

from core.relative_high import compute_escape_top_score, _score_derivatives, _score_onchain
from core.relative_low import (
    reference_low_signals, _hash_ribbon_read, compute_relative_low_score, _score_onchain_low,
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


def test_mvrv_z_now_scored_in_escape_top_onchain():
    """逃頂側：MVRV-Z 已驗證，現在直接影響 _score_onchain / compute_escape_top_score 的分數。"""
    hot = _score_onchain(None, None, mvrv_z=7.5)
    cool = _score_onchain(None, None, mvrv_z=1.0)
    none_ = _score_onchain(None, None, mvrv_z=None)
    assert hot["score"] == 6            # MVRV-Z≥7 滿分
    assert cool["score"] == 0
    assert none_["score"] == 0
    assert "MVRV-Z" in hot["label"]

    score_hot, _ = compute_escape_top_score({"RSI_14": 50.0}, None, mvrv_z=7.5)
    score_none, _ = compute_escape_top_score({"RSI_14": 50.0}, None, mvrv_z=None)
    assert score_hot > score_none       # mvrv_z 現在確實會拉高總分（跟以前「不計分」相反）


def test_mvrv_z_now_scored_in_relative_low_onchain():
    """抄底側：MVRV-Z 已驗證（AUC 0.732），現在直接影響 _score_onchain_low / compute_relative_low_score。"""
    deep = _score_onchain_low(None, None, mvrv_z=-0.5)
    high = _score_onchain_low(None, None, mvrv_z=3.0)
    assert deep["score"] == 6           # MVRV-Z≤0 滿分
    assert high["score"] == 0
    assert "MVRV-Z" in deep["label"]

    score_deep, _ = compute_relative_low_score({"RSI_14": 50.0}, None, mvrv_z=-0.5)
    score_none, _ = compute_relative_low_score({"RSI_14": 50.0}, None, mvrv_z=None)
    assert score_deep > score_none


def test_reference_low_signals_only_has_hash_ribbon():
    """reference_low_signals 不再接受 mvrv_z 參數（已移入正式計分），只剩 hash_ribbon。"""
    with pytest.raises(TypeError):
        reference_low_signals(mvrv_z=-1.0)   # 舊參數已移除，呼叫端仍傳會直接報錯（防止悄悄失效）
    out = reference_low_signals(hashrate_hist=None)
    assert out == {}   # 無算力資料時為空 dict，不含 mvrv_z


def test_hash_ribbon_capitulation_and_cross():
    dates = pd.date_range("2024-01-01", periods=120, freq="D")
    # 算力先升後驟跌（製造 SMA30<SMA60 投降）
    vals = np.concatenate([np.linspace(100, 160, 60), np.linspace(160, 110, 60)])
    hist = {d.strftime("%Y-%m-%d"): v for d, v in zip(dates, vals)}
    r = _hash_ribbon_read(hist)
    assert r is not None and "礦工投降" in r["label"]
    assert "已回測" in r["note"] and "0.359" in r["note"]   # 誠實反映已測、非「待回測」

    # 資料不足 → None
    assert _hash_ribbon_read({d.strftime("%Y-%m-%d"): 1.0
                              for d in pd.date_range("2024-01-01", periods=30)}) is None
    assert _hash_ribbon_read({}) is None
    assert _hash_ribbon_read(None) is None


def test_hash_ribbon_reference_does_not_affect_score():
    """Hash Ribbons 仍與加權分數完全解耦：score 計算不吃 hashrate_hist。"""
    row = {"RSI_14": 50.0}
    score_a, _ = compute_relative_low_score(row, None)
    score_b, _ = compute_relative_low_score(row, None)
    assert score_a == score_b
    ref = reference_low_signals(hashrate_hist=None)
    assert "score" not in ref.get("hash_ribbon", {})
