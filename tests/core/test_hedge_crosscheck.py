#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/core/test_hedge_crosscheck.py — 套保建倉哨兵的「兩源對拍」守門（2026-09-03）

規則（使用者指示）：**兩源都通過才發建倉推播**。
主源＝fetch_market_data 日線；對拍源＝15m DB 重採樣成 1D（回測用的那一套）。

為什麼需要：兩源 RSI 路徑依賴、門檻附近會分岔。2026 年 246 天實測，
對門檻的判定不同 —— 65:2 天／55:2 天／50:1 天，而分岔的日子正好是要下單的日子。
2026-09-02 就是其中一天（主源 64.83 觸發、對拍 65.26 未觸發，門檻 65 夾在中間）。

三條紅線，缺一都會出事：
  1. 分歧時**不可**推建倉  ——會叫使用者下一張沒被回測驗證過的單
  2. 分歧時**不可靜默**    ——2026-08-25~09-02 哨兵靜默 8 天的教訓
  3. 警示旗標**不可**擋住後續正式推播 ——否則分歧解除當天就永遠錯過了
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import scripts.daily_line_notify as dln


@pytest.fixture
def harness(monkeypatch):
    """把外部效應全部攔下來，只留判定邏輯。"""
    sent, saved = [], {}
    state = {}

    monkeypatch.setattr(dln, "_load_escape_state", lambda: dict(state))
    monkeypatch.setattr(dln, "_save_escape_state", lambda s: saved.update(s))
    monkeypatch.setattr(dln, "send_line_message", lambda m: sent.append(m["text"]))

    def run(main_rsi, main_peak, x_rsi, x_peak, st=None):
        state.clear()
        state.update(st or {})
        sent.clear()
        saved.clear()
        monkeypatch.setattr(dln, "crosscheck_daily_rsi",
                            lambda *a, **k: (x_rsi, x_peak, "2026-09-02"))
        dln.maybe_send_hedge_batch_alert({
            "rsi14_closed": main_rsi, "rsi_peak": main_peak,
            "current_price": 77340.0, "rsi_closed_date": "2026-09-02",
        })
        return sent, saved

    return run


# --- 紅線 1：分歧時不可推建倉 ------------------------------------------------

def test_divergence_does_not_send_build_alert(harness):
    """2026-09-02 真實情境：主源 64.83 觸發、對拍 65.26 未觸發 → 不可推建倉。"""
    sent, saved = harness(64.83, 86.01, 65.26, 85.95)
    assert len(sent) == 1
    assert "先不要建倉" in sent[0]
    assert "第 1 批觸發" not in sent[0]
    assert not saved.get("hedge_batch_1"), "分歧時不可把批次標記成已建，否則真觸發日會被靜音"


def test_crosscheck_unavailable_blocks_build_alert(harness):
    """對拍源讀不到（15m DB 缺）→ 也不推建倉：無法驗證就不算通過。"""
    sent, saved = harness(64.83, 86.01, None, None)
    assert "先不要建倉" in sent[0]
    assert "對拍源不可得" in sent[0]
    assert not saved.get("hedge_batch_1")


def test_crosscheck_g3_premise_fails_blocks(harness):
    """對拍源的 G3 前提不成立（近 20 日峰值沒過 75）→ 同樣擋住。"""
    sent, saved = harness(64.83, 86.01, 60.0, 70.0)
    assert "先不要建倉" in sent[0]
    assert not saved.get("hedge_batch_1")


# --- 紅線 2：分歧時不可靜默 --------------------------------------------------

def test_divergence_is_never_silent(harness):
    """踩線卻不推建倉時，必須推一則警示——不可什麼都不做。"""
    sent, _ = harness(64.83, 86.01, 65.26, 85.95)
    assert len(sent) == 1, "分歧時完全不出聲＝哨兵靜默，正是 2026-08 那次的失敗模式"


def test_divergence_warns_only_once_per_batch(harness):
    """已警示過就不重複推（不洗版）。"""
    sent, _ = harness(64.83, 86.01, 65.26, 85.95,
                      st={"hedge_batch_1_xcheck_warned": True})
    assert sent == []


# --- 紅線 3：警示旗標不可擋住後續正式推播 ------------------------------------

def test_warned_flag_does_not_block_real_alert(harness):
    """分歧解除當天，即使先前已警示過，仍必須推正式建倉。"""
    sent, saved = harness(64.20, 86.01, 64.10, 85.95,
                          st={"hedge_batch_1_xcheck_warned": True})
    assert "第 1 批觸發" in sent[0] and "兩源對拍通過" in sent[0]
    assert saved.get("hedge_batch_1") is True


# --- 正常路徑 ----------------------------------------------------------------

def test_both_sources_pass_sends_build_alert(harness):
    sent, saved = harness(64.20, 86.01, 64.10, 85.95)
    assert "第 1 批觸發" in sent[0]
    assert "兩源對拍通過" in sent[0]
    assert saved.get("hedge_batch_1") is True


def test_already_built_batch_is_skipped(harness):
    """已建過的批次不再推（既有行為，回歸守門）。"""
    sent, _ = harness(64.20, 86.01, 64.10, 85.95, st={"hedge_batch_1": True})
    assert sent == []


def test_main_source_g3_premise_fails_short_circuits(harness):
    """主源 G3 前提不成立時直接 return，連對拍都不用做（既有行為）。"""
    sent, _ = harness(64.20, 70.0, 64.10, 85.95)
    assert sent == []
