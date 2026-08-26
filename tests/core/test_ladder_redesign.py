"""
階梯重訂（2026-08-25）單元測試 — 見 tests/ladder_redesign_calib.py 與 tests/low_meta_recalib.py。

守四件事：
  1. 共用級距只有一組，且是共用的（不得逐項調參）
  2. 三個採用的子項（抄底 RSI／SOPR／F&G）分位真的會補分，且不餵歷史時退回舊行為
  3. 冪律子項已移除計分（常亮無鑑別力 + 標籤誤導 + 該檔結構上不可觸及）
  4. 分級門檻不得再出現「歷史上永遠碰不到」的檔位
"""
import pytest

from core.pit_ladder import (pit_percentile, percentile_score,
                             LADDER_HIGH, LADDER_LOW, DEFAULT_WINDOW)
from core.relative_low import (_score_cycle, _score_technical_low, _score_sentiment_low,
                               _score_onchain_low, relative_low_meta, WEIGHTS_LOW)
from core.relative_high import escape_top_meta


# ── 1. 共用級距 ───────────────────────────────────────────────────────────────
def test_ladder_is_shared_and_mirrored():
    """高/低級距必須互為鏡像且只有一組——逐項調參正是 2026-08-25 撤回事件的成因。"""
    assert [f for _, f in LADDER_HIGH] == [f for _, f in LADDER_LOW]
    assert [t for t, _ in LADDER_HIGH] == [100 - t for t, _ in LADDER_LOW]


def test_percentile_score_monotone_and_bounded():
    for mx in (4, 6, 10):
        prev = -1
        for p in (100, 96, 92, 85, 70, 55, 40, 0):
            s = percentile_score(p, mx, high_is_extreme=True)
            assert 0 <= s <= mx
            assert s <= prev or prev == -1 or s <= mx
            prev = s
        assert percentile_score(100, mx, True) == mx
        assert percentile_score(0, mx, True) == 0
        assert percentile_score(0, mx, False) == mx      # 低極端側鏡像
        assert percentile_score(None, mx, True) == 0


# ── 2. 三個採用的子項 ─────────────────────────────────────────────────────────
def _hist(v, n=400):
    return [float(v)] * n


class _Row(dict):
    pass


def test_low_rsi_percentile_adds_score_and_is_backward_compatible():
    """RSI 35 在「長期高檔」的環境屬相對低 → 分位補分；沒有 df 時退回絕對階梯（0 分）。"""
    import pandas as pd
    row = _Row({"RSI_14": 35.0})
    df = pd.DataFrame({"RSI_14": [70.0] * 399 + [35.0]})
    assert _score_technical_low(row, None)["sub"]["rsi_score"] == 0     # 絕對階梯：>30 → 0 分
    assert _score_technical_low(row, df)["sub"]["rsi_score"] > 0        # 分位：本環境極低 → 補分


def test_low_fng_and_sopr_percentile_backward_compatible():
    """不餵歷史 → 與舊絕對階梯逐項相同。"""
    assert _score_sentiment_low(35, None)["sub"]["fng_score"] == 0
    assert _score_sentiment_low(35, None, _hist(70))["sub"]["fng_score"] > 0
    assert _score_onchain_low(None, 1.00)["sub"]["sopr_score"] == 0
    assert _score_onchain_low(None, 1.00, sopr_hist=_hist(1.20))["sub"]["sopr_score"] > 0


def test_percentile_never_lowers_absolute_score():
    """取大值：分位只補分，不得壓低絕對階梯已給的分。"""
    deep = _score_onchain_low(None, 0.90, sopr_hist=_hist(0.80))["sub"]["sopr_score"]
    assert deep == 4                       # 絕對階梯滿分，不因分位不極端而被壓低


# ── 3. 冪律移除 ───────────────────────────────────────────────────────────────
def test_powerlaw_removed_from_scoring():
    """冪律不得再計分，也不得出現在面板文字；原始值保留供機器讀取。"""
    row = _Row({"Mayer_Multiple": 0.7, "SMA200W_Ratio": 0.9, "PowerLaw_Ratio": 0.5})
    res = _score_cycle(row)
    assert res["sub"]["powerlaw_score"] == 0
    assert res["sub"]["powerlaw"] == 0.5
    assert "冪律" not in res["label"] and "冪律" not in res["value"]
    # 兩個子項滿分即為維度滿分（13+12=25）→ 沒有殘留給冪律的分數
    assert res["score"] == WEIGHTS_LOW["cycle"] == 25


def test_cycle_weights_still_sum_to_dimension_max():
    """重分配後上限不變，總分刻度才不會被靜默改動。"""
    row = _Row({"Mayer_Multiple": 9.9, "SMA200W_Ratio": 9.9, "PowerLaw_Ratio": 0.5})
    assert _score_cycle(row)["score"] == 0


# ── 4. 分級門檻不得有死檔位 ───────────────────────────────────────────────────
@pytest.mark.parametrize("meta,ceiling,name", [
    (escape_top_meta, 55, "逃頂"),
    (relative_low_meta, 65, "抄底"),
])
def test_meta_top_levels_are_reachable(meta, ceiling, name):
    """
    最高級必須在「歷史實測上限」之內。原本兩側都用 >=75，但實測上限只有 55／65
    → 兩個等級結構上永遠說不出口（與資費階梯、冪律同型的死檔位問題）。
    ceiling 為 2019-09~2026-08 實測最高分，日後資料長大可上修，但**不可改回不可觸及的值**。
    """
    top_label = meta(ceiling)[0]
    assert "強" in top_label, f"{name}側最高級在實測上限 {ceiling} 分仍到不了：{top_label}"


def test_meta_is_monotone():
    for meta in (escape_top_meta, relative_low_meta):
        seen = [meta(s)[0] for s in range(0, 101)]
        assert len(set(seen)) >= 4          # 至少四個等級真的都會出現


# ── 2026-08-25 獨立檢核補洞：M1/M5 兩個突變原本全綠（測到的是已不計分的那份實作）──
def test_pit_ladder_window_truncation_is_enforced():
    """
    `pit_ladder.pit_percentile` 必須只看最近 window 筆。
    原本只有 `relative_low.funding_pit_percentile` 有測，而那份**已不影響任何分數**；
    真正被三個採用子項使用的是 pit_ladder 這條路，拿掉截斷時 489 全綠（突變 M1）。
    """
    old, recent = [-50.0] * 800, [10.0] * 365
    assert pit_percentile(old + recent, 0.0, min_obs=180, window=365) == 0.0
    assert pit_percentile(old + recent, 0.0, min_obs=180, window=1165) > 50.0
    assert pit_percentile([1.0] * 500, 1.0, min_obs=180, window=10) is None


def test_low_rsi_takes_max_not_percentile_only():
    """
    RSI 子項必須是 max(絕對, 分位)。突變 M5（改成只取分位）原本 489 全綠。
    構造：RSI 18（絕對階梯滿分 6）但所在環境更低 → 分位分較低，取大值仍須是 6。
    """
    import pandas as pd
    row = _Row({"RSI_14": 18.0})
    df = pd.DataFrame({"RSI_14": [10.0] * 399 + [18.0]})   # 環境長期更低 → 分位不極端
    res = _score_technical_low(row, df)
    assert res["sub"]["rsi_score"] == 6, "取大值被改成只取分位"


def test_rsi_percentile_can_be_disabled_for_non_btc():
    """級距在 BTCUSDT 上校準 → 非 BTC 幣對必須關得掉（獨立檢核 🟠 No.7）。"""
    import pandas as pd
    row = _Row({"RSI_14": 35.0})
    df = pd.DataFrame({"RSI_14": [70.0] * 399 + [35.0]})
    on = _score_technical_low(row, df, rsi_pct_enabled=True)["sub"]["rsi_score"]
    off = _score_technical_low(row, df, rsi_pct_enabled=False)["sub"]["rsi_score"]
    assert on > 0 and off == 0


def test_action_ensemble_thresholds_are_reachable():
    """
    行動建議的門檻必須可觸及。原本硬編 ESCAPE_HOT=60 / LOW_STRONG=75，
    都在實測上限（55／65）之上 → TAKE_PROFIT / REDUCE / BOTTOM_FISH 三個分支永遠走不到，
    而這支輸出的是行動短語＋**建議倉位**，被 dashboard 與 LINE 推播消費。
    """
    from core import action_ensemble as AE
    from core.relative_high import TOP_LEVEL_HOT, TOP_LEVEL_WARM
    from core.relative_low import LOW_LEVEL_STRONG, LOW_LEVEL_VALUE
    assert AE.ESCAPE_HOT <= 55 and AE.LOW_STRONG <= 65
    # 必須是 import 來的同一組值，不可再抄一份
    assert (AE.ESCAPE_HOT, AE.ESCAPE_WARM) == (TOP_LEVEL_HOT, TOP_LEVEL_WARM)
    assert (AE.LOW_STRONG, AE.LOW_VALUE) == (LOW_LEVEL_STRONG, LOW_LEVEL_VALUE)


# ── 2026-08-26：抄底最低端新增「實證否決區」（唯一通過獨立驗收的雷達用法）──────
def test_low_veto_zone_is_separated_from_unvalidated_band():
    """
    <=LOW_VETO_VALIDATED 與 6~25 必須是**不同**等級。

    兩段的證據力天差地遠：<=5 有 BTCUSDT 設計 + 非 BTC 幣對 4/4 獨立驗收
    （ETH/SOL/BNB/XRP，跨檔中位 -16.0%）；6~25 完全沒過驗證
    （<=10/15/20/25 的否決比例 44~71%，事前判準 V3 全未過）。
    合併顯示會讓使用者以為兩段有同等證據力 → 本測試就是防止日後有人「順手合併」。
    """
    from core.relative_low import relative_low_meta, LOW_VETO_VALIDATED, LOW_LEVEL_NEUTRAL
    veto = relative_low_meta(LOW_VETO_VALIDATED)[0]
    unvalidated = relative_low_meta(LOW_VETO_VALIDATED + 1)[0]
    assert veto != unvalidated, "否決區與未驗證區被合併了"
    assert "否決" in veto, f"最低級應標示為否決區，實得：{veto}"
    # 邊界：5 在否決區、6 不在；26 以上不受影響
    assert relative_low_meta(0)[0] == veto
    assert relative_low_meta(LOW_LEVEL_NEUTRAL - 1)[0] == unvalidated
    assert relative_low_meta(LOW_LEVEL_NEUTRAL)[0] != unvalidated


def test_low_veto_action_states_the_evidence():
    """否決區的操作建議必須帶數字——沒有數字的警語會被當成語氣詞忽略。"""
    from core.relative_low import relative_low_meta
    action = relative_low_meta(0)[2]
    assert "不進場" in action
    assert "%" in action, "否決建議應附實證數字，否則與一般警語無異"
