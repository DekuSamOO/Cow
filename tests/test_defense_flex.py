# -*- coding: utf-8 -*-
"""
tests/test_defense_flex.py — 防守警報 LINE Flex 守門（2026-11-13）

守四件事：
  1. Flex 內容由 DEFENSE_LADDER 動態組裝：每階觸發價／強平價都在，且不寫死數字
  2. 階的標記隨現價：已觸發 🔴、未觸發 ⚪（同 build_defense_message 的語義）
  3. 版面基本合法：altText ≤ 400、沒有空字串 text、payload 遠低於 LINE 50KB 上限
  4. 發送降級：Flex 推送失敗時退回純文字再送一次（防守警報不可丟失）

數字一律由 config 動態取得，本檔不寫任何真實部位數字（CLAUDE.md 陷阱：公開版控只寫顯假值）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ALERT_PRICE_LOW, DEFENSE_LADDER
from service.notification.builders import build_defense_flex, _payload_size_bytes


def _texts(node):
    """遞迴收集 Flex 樹裡所有 text 元件的字串。"""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node.get("text"))
        for v in node.values():
            out += _texts(v)
    elif isinstance(node, list):
        for v in node:
            out += _texts(v)
    return out


def _stage_header(flex, i):
    return next(t for t in _texts(flex) if t and t.endswith(f"第{i}階"))


def test_flex_contains_every_stage_trigger_and_liq():
    flex = build_defense_flex(ALERT_PRICE_LOW - 1, now_str="TEST")
    blob = json.dumps(flex, ensure_ascii=False)
    for trig, _action, add_btc, liq_after, _note in DEFENSE_LADDER:
        assert f"${trig:,.0f}" in blob, f"Flex 缺觸發價 {trig}"
        assert f"${liq_after:,.0f}" in blob, f"Flex 缺強平價 {liq_after}"
        assert f"+{add_btc} BTC" in blob, f"Flex 缺注資量 {add_btc}"
    assert "final_low" in blob, "Flex 缺條件式提醒（執行前看模型熊底）"
    assert "執行前必對帳重算" in blob, "Flex 缺馬丁重啟即作廢的警語"


def test_flex_stage_marks_follow_price():
    trig1, trig2 = DEFENSE_LADDER[0][0], DEFENSE_LADDER[1][0]
    mid = (trig1 + trig2) / 2          # 只有第 1 階已觸發
    flex = build_defense_flex(mid, now_str="TEST")
    assert _stage_header(flex, 1).startswith("🔴")
    assert all(_stage_header(flex, i).startswith("⚪") for i in range(2, len(DEFENSE_LADDER) + 1))
    below_all = DEFENSE_LADDER[-1][0] - 1
    flex2 = build_defense_flex(below_all, now_str="TEST")
    assert all(_stage_header(flex2, i).startswith("🔴") for i in range(1, len(DEFENSE_LADDER) + 1))


def test_flex_layout_is_line_safe():
    flex = build_defense_flex(DEFENSE_LADDER[-1][0] - 1, now_str="TEST")
    assert flex["type"] == "flex" and flex["contents"]["type"] == "bubble"
    assert 0 < len(flex["altText"]) <= 400
    assert all(isinstance(t, str) and t.strip() for t in _texts(flex)), "Flex 不可有空字串 text（LINE 會回 400）"
    assert _payload_size_bytes(flex) < 40 * 1024


def test_notify_defense_line_falls_back_to_text_when_flex_rejected(monkeypatch):
    """Flex 被 LINE 拒（回 False）→ 改送純文字；兩次呼叫的第一筆是 flex、第二筆是 text。"""
    from service.notification import facade
    sent = []

    def fake_send(messages):
        sent.append(messages[0]["type"])
        return messages[0]["type"] == "text"      # Flex 失敗、文字成功

    monkeypatch.setattr(facade._core, "_send_defense_line_message", fake_send)
    monkeypatch.setattr(facade, "_is_telegram_configured", lambda: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = facade.notify_defense_line(ALERT_PRICE_LOW - 1)
    assert sent[0] == "flex" and sent[1] == "text"
    assert res["line"] is True and res["format"] == "text"


def test_notify_defense_line_sends_flex_first(monkeypatch):
    from service.notification import facade
    sent = []
    monkeypatch.setattr(facade._core, "_send_defense_line_message",
                        lambda m: sent.append(m[0]["type"]) or True)
    monkeypatch.setattr(facade, "_is_telegram_configured", lambda: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = facade.notify_defense_line(ALERT_PRICE_LOW - 1)
    assert sent[0] == "flex", "首響應為 Flex"
    assert all(t == "text" for t in sent[1:]), "三連響後續短訊息維持純文字"
    assert res["format"] == "flex" and res["burst"] == len(sent) - 1
