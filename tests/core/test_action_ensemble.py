"""core/action_ensemble 三軸合成決策矩陣測試。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from core.action_ensemble import compute_composite_action, compute_trend_stance
from core.action_ensemble import (LOW_STRONG as _LOW_STRONG, LOW_VALUE as _LOW_VALUE,
                                  ESCAPE_HOT as _ESCAPE_HOT, ESCAPE_WARM as _ESCAPE_WARM)

# 「達到明確低估但**未**到強力抄底」的代表值——原本寫死 65，
# 但門檻 2026-08-25 重校後 65 已越過 LOW_STRONG，語意會反轉（獨立檢核 🟠 No.1 的連帶）。
_LOW_VALUE_ONLY = _LOW_VALUE if _LOW_VALUE < _LOW_STRONG else _LOW_STRONG - 1


# 2026-08-25：本矩陣原本寫死 70/50/65/80 等分數。門檻重校後（ESCAPE_HOT 60→49、
# LOW_STRONG 75→56）那些值的**語意會反轉**（65 從「低估」變成「強力低估」），
# 測試就會在行為正確時誤報失敗 → 一律改用從常數推導的代表值。
_ESC_HOT = _ESCAPE_HOT              # 已達「明確過熱」
_ESC_WARM_ONLY = _ESCAPE_WARM       # 達偏熱但未到過熱
_ESC_CALM = 10                      # 中性
_LOW_STRONG_V = _LOW_STRONG         # 已達「強力抄底」
_LOW_CALM = 10


@pytest.mark.parametrize("trend,esc,low,expected_key", [
    # 多頭分支
    (50, _ESC_HOT, _LOW_CALM, "TAKE_PROFIT"),          # 強多＋過熱 → 分批止盈
    (50, _ESC_WARM_ONLY, _LOW_CALM, "HOLD_TIGHTEN"),   # 多頭偏熱（未到過熱）
    (30, _ESC_CALM, _LOW_VALUE_ONLY, "ADD"),           # 多頭仍低估 → 加倉
    (30, _ESC_CALM, _LOW_CALM, "RIDE"),                # 多頭中性
    # 空頭分支
    (-50, _ESC_CALM, _LOW_STRONG_V, "BOTTOM_FISH"),    # 空頭＋強力低估 → 小倉左側
    (-50, _ESC_CALM, _LOW_VALUE_ONLY, "WATCH_REVERSAL"),  # 空頭＋低估 → 等右側（勿接刀）
    (-30, _ESC_HOT, _LOW_CALM, "FADE_RALLY"),          # 空頭反彈過熱
    (-30, _ESC_CALM, _LOW_CALM, "DEFENSE"),            # 空頭中性
    # 盤整分支
    (0, _ESC_HOT, _LOW_CALM, "REDUCE"),
    (0, _ESC_CALM, _LOW_VALUE_ONLY, "ACCUMULATE"),
    (0, _ESC_CALM, _LOW_CALM, "RANGE"),
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


def test_boundary_alignment_with_meta_levels():
    """
    行動門檻必須與 meta 分級邊界一致 —— **從常數推導、不寫死數字**。
    2026-08-25：原本寫死 60（並註解「與 LINE 警報門檻一致」），但 60 在逃頂實測上限 55
    之上 → TAKE_PROFIT 分支永遠走不到；同時該註解也早已漂移（LINE 主閘門實際是 45）。
    現在 ESCAPE_HOT 直接 import core.relative_high.TOP_LEVEL_HOT，兩邊不可能再各走各的。
    """
    from core.action_ensemble import ESCAPE_HOT
    from core.relative_high import TOP_LEVEL_HOT
    assert ESCAPE_HOT == TOP_LEVEL_HOT
    assert compute_composite_action(30, ESCAPE_HOT, 0)["action_key"] == "TAKE_PROFIT"
    assert compute_composite_action(30, ESCAPE_HOT - 1, 0)["action_key"] == "HOLD_TIGHTEN"


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


def test_notes_absent_by_default():
    # 不傳 notes → confidence_note 為 None，行為與舊版完全相同（向後相容）
    out = compute_composite_action(30, _ESC_CALM, _LOW_CALM)
    assert out["confidence_note"] is None


@pytest.mark.parametrize("trend,esc,low,bucket", [
    (30, _ESC_HOT, _LOW_CALM, "escape_driven"),       # TAKE_PROFIT
    (-30, _ESC_HOT, _LOW_CALM, "escape_driven"),      # FADE_RALLY
    (30, _ESC_CALM, _LOW_VALUE_ONLY, "value_driven"), # ADD
    (-50, _ESC_CALM, _LOW_STRONG_V, "value_driven"),  # BOTTOM_FISH
    (30, _ESC_CALM, _LOW_CALM, "trend_only"),         # RIDE
    (-30, _ESC_CALM, _LOW_CALM, "trend_only"),        # DEFENSE
    (0, _ESC_CALM, _LOW_CALM, "trend_only"),          # RANGE
])
def test_confidence_note_bucket(trend, esc, low, bucket):
    notes = {"escape_driven": "ESC", "value_driven": "VAL", "trend_only": "TREND"}
    out = compute_composite_action(trend, esc, low, notes=notes)
    assert out["confidence_note"] == notes[bucket]


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
