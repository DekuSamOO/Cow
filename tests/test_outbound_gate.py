# -*- coding: utf-8 -*-
"""tests/test_outbound_gate.py — 對外推播閘門（2026-09-04 立）。

背景：2026-09-02 一個 subagent 在本機直接跑 `scripts/daily_line_notify.py`，
**真的把當日 Flex 卡片推到使用者手機**。成因是 `__main__` 沒有 dry_run 參數、
憑證由 `.env` 的 load_dotenv() 自動載入——「在本機試一下」等於真的送出去。
全域規則 §0.4 擋不住（規則是建議性的），故加確定性閘門：本機預設不送。

三條紅線：
  1. 本機（非 CI、未設 DRY_RUN）**絕不可**送出 —— 這是事故本身
  2. GitHub Actions 內**必須**照送 —— 擋過頭等於哨兵停擺
  3. 閘門必須蓋住**每一條**對外路徑（日常 LINE／防守 LINE／Telegram）
"""
import pytest

from service.notification import core as nc


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每個測試都從乾淨環境開始——本機跑測試時 GITHUB_ACTIONS 不存在，CI 上存在。"""
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def spy(monkeypatch):
    """攔截真正的 HTTP，任何一次呼叫都代表「訊息真的送出去了」。"""
    calls = []
    monkeypatch.setattr(nc, "safe_post",
                        lambda *a, **k: calls.append((a, k)) or _Resp())
    # 讓憑證檢查通過，才測得到閘門本身而不是「未設定所以跳過」
    monkeypatch.setattr(nc, "LINE_CHANNEL_ACCESS_TOKEN", "tok-placeholder")
    monkeypatch.setattr(nc, "LINE_USER_ID", "uid-placeholder")
    monkeypatch.setattr(nc, "TELEGRAM_BOT_TOKEN", "tg-placeholder")
    monkeypatch.setattr(nc, "TELEGRAM_CHAT_ID", "chat-placeholder")
    return calls


class _Resp:
    status_code = 200
    text = "ok"


MSG = [{"type": "text", "text": "測試訊息"}]


# ── 紅線 1：本機絕不可送 ────────────────────────────────────────────────
def test_local_run_is_blocked(spy):
    """事故重演：本機直接呼叫，必須擋下且不得發出任何 HTTP。"""
    assert nc._send_line_message(MSG) is False
    assert spy == [], "本機推播沒被擋下 —— 這正是 2026-09-02 事故的成因"


def test_defense_channel_also_blocked(spy, monkeypatch):
    """紅線 3：防守通道不可繞過閘門（它 fallback 到日常通道，兩條都要擋）。"""
    monkeypatch.setattr(nc, "DEFENSE_LINE_CHANNEL_ACCESS_TOKEN", "d-tok")
    monkeypatch.setattr(nc, "DEFENSE_LINE_USER_ID", "d-uid")
    assert nc._send_defense_line_message(MSG) is False
    assert spy == []


def test_telegram_also_blocked(spy):
    """紅線 3：只擋 LINE 會留下另一條對外的路。"""
    assert nc._send_telegram_message("測試") is False
    assert spy == []


# ── 紅線 2：CI 必須照送 ─────────────────────────────────────────────────
def test_github_actions_is_allowed(spy, monkeypatch):
    """擋過頭等於哨兵停擺 —— GitHub Actions 內必須真的送。"""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert nc._send_line_message(MSG) is True
    assert len(spy) == 1, "CI 內被誤擋，每日哨兵會靜默"


# ── DRY_RUN 明確設值時優先於環境判斷 ────────────────────────────────────
def test_explicit_dry_run_0_allows_local_send(spy):
    """本機要真送必須明確寫 DRY_RUN=0 —— 這個顯式性本身就是「核准」。"""
    import os
    os.environ["DRY_RUN"] = "0"
    try:
        assert nc._send_line_message(MSG) is True
        assert len(spy) == 1
    finally:
        os.environ.pop("DRY_RUN", None)


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_dry_run_truthy_blocks_even_in_ci(spy, monkeypatch, val):
    """DRY_RUN 明確設值時優先於 GITHUB_ACTIONS —— CI 上也要能演練。"""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("DRY_RUN", val)
    assert nc._send_line_message(MSG) is False
    assert spy == []


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", " 0 "])
def test_dry_run_falsy_values_allow_send(spy, monkeypatch, val):
    monkeypatch.setenv("DRY_RUN", val)
    assert nc._send_line_message(MSG) is True
    assert len(spy) == 1


def test_empty_dry_run_falls_back_to_env_detection(spy, monkeypatch):
    """DRY_RUN='' 視為沒設（CI 常見的空字串注入），不可被當成 truthy 而誤擋 CI。"""
    monkeypatch.setenv("DRY_RUN", "")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert nc._send_line_message(MSG) is True
    assert len(spy) == 1


# ── 擋下時必須留下痕跡（絕不靜默）──────────────────────────────────────
def test_blocked_send_is_not_silent(spy, capsys):
    """靜默的閘門會讓人以為訊息送出去了 —— 必須印出擋下原因與內容摘要。"""
    nc._send_line_message([{"type": "text", "text": "這串要出現在預覽裡"}])
    out = capsys.readouterr().out
    assert "閘門擋下" in out
    assert "這串要出現在預覽裡" in out, "擋下時沒顯示本來要送什麼，等於資訊消失"
