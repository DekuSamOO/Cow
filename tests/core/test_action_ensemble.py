"""core/action_ensemble 三軸合成決策矩陣測試。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from core.action_ensemble import compute_composite_action


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
