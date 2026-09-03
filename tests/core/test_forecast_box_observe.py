#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/core/test_forecast_box_observe.py — `_build_forecast_box` 的 observe 分支守門
（2026-09-02，四季論 v2 切換前的消費端盤點補洞）

**為什麼有這支測試**：`_build_forecast_box` 原本是二元判斷
（`forecast_type == "bear_bottom"` 為熊、**其餘全部當牛**），
v2 的 `observe` 型會掉進 else 分支，渲染成「🚀 牛市最高價預測」；
再配上 `daily_line_notify` 已把 `target_* = None` 轉成 `0`，
實際推出去會是「🚀 牛市最高價預測 $0 / $0 / $0」——
不崩潰，但**是一張看起來很確定、方向卻完全相反的卡**。

2026-07-06 的消費端盤點只補了「不崩潰」，沒補「不誤導」。
這支測試把「不誤導」釘住。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

from service.notification.builders import _build_forecast_box


def _base(ftype):
    """模擬 daily_line_notify 餵進來的 summary（含它對 None 的 0 轉換）。"""
    return {
        "forecast_type": ftype,
        "target_low": 0, "target_median": 0, "target_high": 0,
        "label_low": "最深", "label_high": "最淺",
        "forecast_ath_ref": None, "forecast_estimated_date": "N/A",
        "forecast_note": "轉折觀察期，不出目標價",
    }


def _text_of(box):
    return json.dumps(box, ensure_ascii=False)


def test_observe_does_not_render_bull_title():
    """observe 不得出現「牛市最高價預測」——這是原 bug 的核心症狀。"""
    t = _text_of(_build_forecast_box(_base("observe")))
    assert "牛市最高價預測" not in t
    assert "熊市最低價預測" not in t
    assert "轉折觀察期" in t


def test_observe_does_not_render_zero_targets():
    """observe 不得出現 $0 價位——None→0 的轉換不該被當成真的目標價印出來。"""
    t = _text_of(_build_forecast_box(_base("observe")))
    assert "$0" not in t


def test_observe_carries_reason():
    """不出目標價時必須說明為什麼（design 誠實原則）。"""
    t = _text_of(_build_forecast_box(_base("observe")))
    assert "轉折觀察期，不出目標價" in t


def test_bear_branch_unchanged():
    """既有熊市分支行為不得改變（回歸守門）。"""
    s = _base("bear_bottom")
    s.update(target_low=20000, target_median=30000, target_high=40000)
    t = _text_of(_build_forecast_box(s))
    assert "熊市最低價預測" in t and "$30,000" in t


def test_bull_branch_unchanged():
    """既有牛市分支行為不得改變（回歸守門）。"""
    s = _base("bull_peak")
    s.update(target_low=100000, target_median=150000, target_high=200000)
    t = _text_of(_build_forecast_box(s))
    assert "牛市最高價預測" in t and "$150,000" in t


# ---------------------------------------------------------------------------
# P-2（2026-09-02 拍板 B 案）：bottom_eval 不受 season_engine 影響，
# observe 時必須在呈現層標注四季論處於觀察期，否則同一張卡自相矛盾
# ---------------------------------------------------------------------------

from service.notification.builders import _build_bottom_eval_box   # noqa: E402


def _eval_base(ftype, basis="四季論趨勢底"):
    return {
        "forecast_type": ftype,
        "current_price": 77000,
        "bottom_eval": {
            "current_price": 77000,
            "final_low": 33035, "final_low_basis": basis,
            "final_low_deep": 24776, "final_low_shallow": 37990,
            "ensemble_low": 60751,
            "estimates": [
                {"key": "season_bottom", "label": "四季論趨勢底", "value": 33035,
                 "kind": "season", "note": "", "reliability": 58},
                {"key": "miner_elec", "label": "礦工電費(硬地板)", "value": 18000,
                 "kind": "floor", "note": "", "reliability": 75},
            ],
        },
    }


def test_observe_flags_season_bottom_as_reference_only():
    """observe ＋ 依據是四季論 → 必須標「轉折觀察期／僅供參考」。"""
    t = _text_of(_build_bottom_eval_box(_eval_base("observe")))
    assert "轉折觀察期" in t and "僅供參考" in t
    assert "$33,035" in t, "B 案是保留數字、只加註記，不是把數字拿掉"


def test_non_observe_has_no_extra_warning():
    """bear_bottom 時不加註記（避免每天都掛一句廢話）。"""
    t = _text_of(_build_bottom_eval_box(_eval_base("bear_bottom")))
    assert "轉折觀察期" not in t


def test_observe_with_non_season_basis_has_no_warning():
    """依據不是四季論（例如落在礦工電費地板）時不加註記——那個數字與象限無關。"""
    t = _text_of(_build_bottom_eval_box(_eval_base("observe", basis="礦工電費硬地板")))
    assert "轉折觀察期" not in t
