"""逃頂/抄底雷達『參考訊號 → 正式計分』轉換單元測試。

2026-07 用 tests/relative_ref_signals_backtest.py 驗證 MVRV-Z 逃頂/抄底皆過 AUC≥0.55
門檻（0.592/0.732），已從「參考顯示、不計分」轉為 onchain 子分正式計分項；
`reference_top_signals`（原本只含 mvrv_z）已整支移除。Hash Ribbons（同批驗證 AUC=0.359，
方向反/無效）原以參考顯示保留，2026-07 亦整段移除（`reference_low_signals` / `_hash_ribbon_read`
連同 watcher 面板顯示一併刪除，見 core/relative_low.py 檔頭）。"""
from core.relative_high import compute_escape_top_score, _score_derivatives, _score_onchain
from core.relative_low import compute_relative_low_score, _score_onchain_low


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
