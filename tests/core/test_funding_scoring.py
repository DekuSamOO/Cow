"""資金費率計分（2026-08-25 重校）單元測試。

守兩件事：
  1. 抄底負費率子項＝「絕對階梯 ∪ PiT 滾動分位」取大值，且**沒餵歷史時行為與舊版逐項相同**
     （向後相容是這次改動的前提：radar_replay / backtest / dashboard 多處呼叫端未餵歷史）。
  2. 逃頂側**不得**被改成相對分位（2026-08-25 校準三處全輸已否決）→ 分數只吃絕對年化，
     餵歷史只會多出休眠標示，分數必須一模一樣。
校準紀錄與拍板理由見 tests/funding_percentile_calib.py 與兩支 core 檔的常數註解。
"""
import pytest

from core.relative_high import (_score_derivatives, funding_dormant_days,
                                FUNDING_ANN_BASELINE, FUNDING_BASELINE_8H, FUNDING_HOT_8H)
from core.relative_low import (_score_derivatives_low, funding_pit_percentile,
                               FUNDING_PCT_MIN_OBS, FUNDING_PCT_WINDOW)

# 年化 = rate_8h × 3 × 365（×1095）
_BASELINE_8H = 0.01      # 幣安 interestRate；年化 10.95% → 逃頂「⚪ 中性」0 分
_DEEP_NEG_8H = -0.02     # 年化 -21.9% → 絕對階梯滿分 10


def _hist(value, n=200):
    return [value] * n


# ── PiT 分位純函數 ────────────────────────────────────────────────────────────
def test_percentile_needs_min_obs():
    """樣本不足 min_obs → None（不給分位分，退回純絕對階梯），不得拿少量樣本硬算。"""
    assert funding_pit_percentile(_hist(5.0, FUNDING_PCT_MIN_OBS - 1)) is None
    assert funding_pit_percentile(_hist(5.0, FUNDING_PCT_MIN_OBS)) is not None
    assert funding_pit_percentile(None) is None


def test_percentile_midrank_and_extremes():
    """同值用 midrank；最低/最高值分別落在兩端。"""
    hist = [float(i) for i in range(100)]
    assert funding_pit_percentile(hist, current=-999) == 0.0
    assert funding_pit_percentile(hist, current=999) == 100.0
    # 全部同值 → midrank 落在正中央，不會被判成極端
    assert funding_pit_percentile(_hist(3.0), current=3.0) == pytest.approx(50.0)


def test_percentile_is_point_in_time():
    """只吃傳入序列（呼叫端負責切片）→ 未來值不影響今日分位，杜絕前視偏差。"""
    past = [0.0] * 100 + [-50.0]
    with_future = past + [-99.0] * 50
    assert funding_pit_percentile(past, current=-50.0) == funding_pit_percentile(
        past, current=-50.0)
    # 同樣的今日值，餵進含「更深負的未來」序列會得到不同分位 → 證明它確實只看被餵的那段
    assert funding_pit_percentile(with_future, current=-50.0) != funding_pit_percentile(
        past, current=-50.0)


# ── 抄底：混合取大值 + 向後相容 ───────────────────────────────────────────────
def test_low_without_history_matches_absolute_ladder():
    """沒餵歷史 → 與舊版絕對階梯逐項相同（向後相容）。"""
    for rate_8h, expected in [(_DEEP_NEG_8H, 10), (-0.01, 8), (-0.005, 6),
                              (-0.002, 3), (-0.0005, 1), (0.0, 0), (_BASELINE_8H, 0)]:
        got = _score_derivatives_low(rate_8h, None)["sub"]["funding_score"]
        assert got == expected, f"{rate_8h}%/8h 應得 {expected} 分，實得 {got}"


def test_low_percentile_never_lowers_absolute_score():
    """分位在任何情況下都不得壓低絕對階梯的分數。"""
    # 深負在「同樣深負滿地」的環境裡分位不極端，但絕對階梯仍必須給滿分
    hist = _hist(-25.0)
    assert _score_derivatives_low(_DEEP_NEG_8H, None, hist)["sub"]["funding_score"] == 10


def test_low_percentile_does_not_affect_score():
    """
    2026-08-25 撤回迴歸：PiT 分位**不得**影響抄底分數（只留 sub 觀測值、不進 label）。
    背景：混合版當初的 holdout 優勢全來自未揭露的自選 cutoff(25.0)；改回稀有度換算值(13.0)
    後 8 個對照種子 0/8 勝 → 依 CONSTITUTION 第 11 條不予採用。
    **要改回混合計分，必須先重跑 funding_percentile_calib 並過 holdout，不能只看 train。**
    """
    hist = _hist(FUNDING_ANN_BASELINE)          # 環境長期貼基準 → 今日淺負在此環境是極冷
    rate_8h = -1.0 / 1095                       # 年化 -1% → 絕對階梯「微負費率」1 分
    with_hist = _score_derivatives_low(rate_8h, None, hist)
    without = _score_derivatives_low(rate_8h, None)
    assert with_hist["sub"]["funding_score"] == without["sub"]["funding_score"] == 1
    assert with_hist["label"] == without["label"]        # 分位不得出現在面板文字
    assert with_hist["sub"]["funding_pct"] is not None   # 但仍輸出觀測值供日後重校
    assert with_hist["sub"]["funding_pct_window"] == FUNDING_PCT_WINDOW


# ── 逃頂：分數不得因歷史而改變，只多休眠標示 ──────────────────────────────────
def test_top_score_unchanged_by_history():
    """逃頂側已否決分位法 → 餵歷史只影響 label，分數必須一致。"""
    for rate_8h in (_BASELINE_8H, 0.0, 0.05, -0.01):
        a = _score_derivatives(rate_8h, None)
        b = _score_derivatives(rate_8h, None, _hist(FUNDING_ANN_BASELINE))
        assert a["score"] == b["score"]
        assert a["sub"]["funding_score"] == b["sub"]["funding_score"]


def test_top_baseline_is_neutral_zero_points():
    """0.01%/8h 是幣安利率基準＝真中性，必須 0 分（勿因為它是近年最大值就給分）。"""
    res = _score_derivatives(FUNDING_BASELINE_8H, None)
    assert res["sub"]["funding_score"] == 0
    assert "中性" in res["label"]


def test_dormancy_label_only_when_no_breach():
    """休眠標示：整段視窗未越基準才標，且只動 label 不動分數。"""
    dormant = _score_derivatives(FUNDING_BASELINE_8H, None, _hist(FUNDING_ANN_BASELINE))
    assert "休眠" in dormant["label"]
    # 視窗內曾越過基準（末日除外）→ 不標休眠
    breached = _hist(FUNDING_ANN_BASELINE)[:-1] + [FUNDING_ANN_BASELINE + 20]
    assert "休眠" not in _score_derivatives(FUNDING_BASELINE_8H, None, breached)["label"]


def test_dormant_days_counts_back_to_last_breach():
    hist = [FUNDING_ANN_BASELINE + 20] + [FUNDING_ANN_BASELINE] * 5
    assert funding_dormant_days(hist) == 5
    assert funding_dormant_days(_hist(FUNDING_ANN_BASELINE, 7)) == 7
    assert funding_dormant_days(None) is None


def test_display_thresholds_derive_from_core_constant():
    """顯示層過熱線收回單一來源（app.py / tab_macro_compass 原各自硬編 0.03）。"""
    from core.relative_high import FUNDING_ANN_YELLOW
    assert FUNDING_HOT_8H == pytest.approx(FUNDING_ANN_YELLOW / 1095)
    assert FUNDING_HOT_8H > FUNDING_BASELINE_8H     # 基準必須低於過熱線


# ── 2026-08-25 獨立檢核抓到的迴歸（視窗未套用／無資料仍回報分位）──────────────
def test_percentile_truncates_to_window():
    """
    呼叫端餵超過 FUNDING_PCT_WINDOW 的歷史時，分位必須只看最近 window 日。
    背景：BTC_WATCH 為了讓休眠天數數得回 624 日前而餵 900 日，同一份 list 被拿去算分位
    → 生產實跑 900 日分位、不是校準拍板的 180 日。視窗收斂在函式內，杜絕再犯。
    """
    old, recent = [-50.0] * 800, [10.0] * 180          # 久遠的深負 + 近期全部貼基準
    # 今日 0.0：只看近 180 日 → 比 180 個 10.0 都低 → 分位 0；若誤用全部 980 日則會被舊資料墊高
    assert funding_pit_percentile(old + recent, current=0.0) == 0.0
    assert funding_pit_percentile(old + recent, current=0.0, window=980) > 50.0
    # 視窗內樣本不足 min_obs 時仍回 None（不可因為 hist 很長就放行）
    assert funding_pit_percentile([1.0] * 500, window=10, min_obs=90) is None


def test_no_percentile_reported_when_funding_missing():
    """資費抓不到（ann=None）時不得回報分位，否則面板會顯示與今日無關的數字。"""
    res = _score_derivatives_low(None, None, _hist(10.95))
    assert res["sub"]["funding_score"] == 0
    assert res["sub"]["funding_pct"] is None
