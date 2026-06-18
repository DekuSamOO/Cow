"""core/action_ensemble 三軸合成決策矩陣測試。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from core.action_ensemble import compute_composite_action, compute_trend_stance


@pytest.mark.parametrize("trend,esc,low,expected_key", [
    # 多頭分支
    (50, 70, 10, "TAKE_PROFIT"),      # 強多＋過熱 → 分批止盈
    (50, 50, 10, "HOLD_TIGHTEN"),     # 多頭偏熱
    (30, 10, 65, "ADD"),              # 多頭仍低估 → 加倉
    (30, 10, 10, "RIDE"),             # 多頭中性
    # 空頭分支
    (-50, 10, 80, "BOTTOM_FISH"),     # 空頭＋強力低估 → 小倉左側
    (-50, 10, 65, "WATCH_REVERSAL"),  # 空頭＋低估 → 等右側（勿接刀）
    (-30, 50, 10, "FADE_RALLY"),      # 空頭反彈過熱
    (-30, 10, 10, "DEFENSE"),         # 空頭中性
    # 盤整分支
    (0, 65, 10, "REDUCE"),
    (0, 10, 65, "ACCUMULATE"),
    (0, 10, 10, "RANGE"),
])
def test_decision_matrix(trend, esc, low, expected_key):
    out = compute_composite_action(trend, esc, low)
    assert out["action_key"] == expected_key
    assert 0 <= out["pos_low"] < out["pos_high"] <= 100
    assert "未擬合" in out["pos_label"]


def test_trend_none_returns_none():
    assert compute_composite_action(None, 70, 10) is None


def test_escape_low_none_treated_as_zero():
    out = compute_composite_action(30, None, None)
    assert out["action_key"] == "RIDE"


def test_boundary_alignment_with_alert_threshold():
    # 逃頂熱門檻與 LINE 警報門檻一致（60）
    assert compute_composite_action(30, 60, 0)["action_key"] == "TAKE_PROFIT"
    assert compute_composite_action(30, 59, 0)["action_key"] == "HOLD_TIGHTEN"


@pytest.mark.parametrize("trend,esc,low,cyc,expected_key", [
    # cycle 深跌（≥22）視同明確低估，與 low≥60 同級觸發
    (-50, 10, 40, 25, "WATCH_REVERSAL"),   # 空頭＋cyc深跌（low 僅 40）→ 等右側（2026-06 $59k 底情境）
    (0,   10, 40, 25, "ACCUMULATE"),       # 盤整＋cyc深跌 → 區間下緣佈局
    (30,  10, 40, 25, "ADD"),              # 多頭＋cyc深跌 → 回踩加倉
    (-50, 10, 40, 18, "DEFENSE"),          # cyc 18<22 不觸發 → 仍防守輕倉（$77k 情境）
])
def test_cycle_deep_value(trend, esc, low, cyc, expected_key):
    assert compute_composite_action(trend, esc, low, cyc)["action_key"] == expected_key


def test_cycle_backward_compatible():
    # 不傳 cycle（None）行為與舊 3-arg 完全相同
    assert compute_composite_action(-50, 10, 40)["action_key"] == "DEFENSE"
    assert compute_composite_action(-50, 10, 40, None)["action_key"] == "DEFENSE"


@pytest.mark.parametrize("trend,mom,expected_key", [
    (73, "🟢 短線偏多", "RIDE_STRONG"),    # 強多頭
    (30, "🔴 短線偏空", "PULLBACK"),       # 多頭但短線轉弱 → 回檔
    (30, "🟢 短線偏多", "RIDE"),           # 多頭順勢
    (-30, "🟢 短線偏多", "BOUNCE"),        # 空頭中的短線反彈
    (-30, "🔴 短線偏空", "REDUCE"),        # 空頭偏空減碼
    (-82, "🔴 短線偏空", "EXIT"),          # 強空頭 → 減碼/出場
    (-2, "⚪ 短線中性", "RANGE"),          # 盤整
])
def test_trend_stance(trend, mom, expected_key):
    assert compute_trend_stance(trend, mom)["action_key"] == expected_key


def test_trend_stance_none():
    assert compute_trend_stance(None) is None
