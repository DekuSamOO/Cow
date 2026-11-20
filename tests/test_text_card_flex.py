# -*- coding: utf-8 -*-
"""通用「文字行 → Flex 卡片」守門（2026-11-20：哨兵純文字推播全面改 Flex）。顯假值。

訊息一律以行陣列組裝再 chr(10).join（與 daily_line_notify 慣例一致，原始碼不出現反斜線）。
"""
import sys
import os
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from service.notification.builders import build_text_card_flex, _CARD_KINDS

NL = chr(10)


def _j(*lines):
    return NL.join(lines)


def _texts(node):
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


def _header_color(msg):
    return msg["contents"]["header"]["backgroundColor"]


@pytest.mark.parametrize("title,kind", [
    ("[開啟] 升槓桿窗口", "leverage"),
    ("[第 3/6 批] 升槓桿窗口", "leverage"),
    ("[暫停] 升槓桿窗口", "leverage"),
    ("[熊底確認] D3 成立", "d3"),
    ("[D3 死結] 情境二目前結構性不可觸發", "d3"),
    ("[D3 網格] 強平緩衝觸發", "defense"),
    ("[網格下限下移] 本波低點創新低", "grid"),
    ("[套保建倉] 第 1 批觸發", "hedge"),
    ("[哨兵失明] 升槓桿窗口哨兵 取不到資料", "system"),
    ("[狀態鏈斷裂] 哨兵還原到的是舊狀態", "system"),
    ("🔐 防守 config 同步驗證", "system"),
    ("🧯 PREREG 事前保險絲：BTC $1 跌破", "fuse"),
    ("🪜 防守階梯到價：BTC $1", "defense"),
    ("⏰ 防守決策窗提醒（第1則）", "defense"),
    ("🔒 防守決策窗關閉（24h 屆滿）", "defense"),
    ("🚨🚨 防守警報 第2響／共3響", "defense"),
])
def test_kind_inferred_from_title(title, kind):
    msg = build_text_card_flex(_j(title, "內容"))
    assert _header_color(msg) == _CARD_KINDS[kind][0]
    assert msg["altText"] == title


def test_explicit_kind_overrides_inference():
    msg = build_text_card_flex(_j("[哨兵失明] x", "y"), kind="fuse")
    assert _header_color(msg) == _CARD_KINDS["fuse"][0]


def test_every_line_survives_and_markdown_is_stripped():
    msg = build_text_card_flex(_j("[第 3/6 批] 升槓桿窗口", "AHR999 0.283", "",
                                  "**不要建倉**，等下一根", "  縮排說明", "━━━━━━",
                                  "※ 附註", "最後一行"))
    body = " ".join(_texts(msg["contents"]["body"]))
    for s in ("AHR999 0.283", "不要建倉，等下一根", "縮排說明", "※ 附註", "最後一行"):
        assert s in body, f"內容遺失：{s}"
    assert "**" not in body and "━" not in body
    seps = [n for n in msg["contents"]["body"]["contents"] if n["type"] == "separator"]
    assert len(seps) == 2, "空行與 ━ 分隔線各轉一條 separator"


def test_bold_spans():
    msg = build_text_card_flex(_j("t", "前 **粗** 後"))
    node = msg["contents"]["body"]["contents"][0]
    assert [s.get("weight") for s in node["contents"]] == [None, "bold", None]
    assert node["text"] == "前 粗 後"


def test_title_only_and_empty():
    msg = build_text_card_flex("🚨🚨 防守警報 第2響")
    assert "🚨🚨 防守警報 第2響" in _texts(msg["contents"]["body"]), "單行訊息內容要進 body"
    assert msg["altText"] == "🚨🚨 防守警報 第2響"
    assert all(t.strip() for t in _texts(msg)), "不可留只有空白的 text"
    with pytest.raises(ValueError):
        build_text_card_flex(_j("", ""))


def test_no_empty_text_nodes():
    """LINE 拒收空字串 text；空行一律轉 separator，不留空 text。"""
    msg = build_text_card_flex(_j("t", "", "", "a", "", "  ", "b"))
    assert all(t for t in _texts(msg))


def _load_notify():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "dln_card", os.path.join(repo, "scripts", "daily_line_notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_daily_send_flex_first_then_text_fallback(monkeypatch):
    notify = _load_notify()
    import service.notification.core as core
    sent = []
    payload = {"type": "text", "text": _j("[哨兵失明] x", "y")}
    monkeypatch.setattr(core, "_send_line_message",
                        lambda m: sent.append(m[0]["type"]) or m[0]["type"] == "text")
    notify.send_line_message(payload)
    assert sent == ["flex", "text"], "Flex 被拒要退回純文字，不可丟訊息"
    sent.clear()
    monkeypatch.setattr(core, "_send_line_message", lambda m: sent.append(m[0]["type"]) or True)
    notify.send_line_message(payload)
    assert sent == ["flex"], "Flex 成功就不再送文字"
    sent.clear()
    notify.send_line_message({"type": "flex", "altText": "a", "contents": {}})
    assert sent == ["flex"], "原本就是 Flex 的照送"
