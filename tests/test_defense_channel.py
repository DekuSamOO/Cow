"""U5-① 防守警報 LINE 重複策略：狀態機/三連響/fallback 測試（不實際發送）。"""
import sys
import os
import json
import importlib.util
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _load_price_alert():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "price_alert", os.path.join(repo, "scripts", "price_alert.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pa():
    return _load_price_alert()


# ── 純函數：里程碑排程 ─────────────────────────────────────────────

def test_due_reminder_idx_progression(pa):
    """每小時 cron 正常跑：1h/2h/3h/6h… 逐則到期。"""
    assert pa._due_reminder_idx(0.5, 0) == 0          # 未到第一個里程碑
    assert pa._due_reminder_idx(1.02, 0) == 1         # 1h 到期
    assert pa._due_reminder_idx(2.05, 1) == 2
    assert pa._due_reminder_idx(4.0, 3) == 3          # 3h 已發、6h 未到 → 無新提醒
    assert pa._due_reminder_idx(23.9, 6) == 6         # 18h 已發、24h 未到


def test_due_reminder_idx_catchup_skips_backlog(pa):
    """Actions 停擺補跑：只推最新一則，不轟炸積欠的中間里程碑。"""
    # 事件後 7 小時才恢復，之前一則都沒發 → idx 直接跳到 4（1/2/3/6h 全過期，只發一次）
    assert pa._due_reminder_idx(7.0, 0) == 4


# ── 純函數：文案 ───────────────────────────────────────────────────

def test_defense_burst_and_reminder_texts():
    from service.notification.facade import (build_defense_burst_texts,
                                             build_defense_reminder,
                                             build_defense_window_close)
    bursts = build_defense_burst_texts(3)
    assert len(bursts) == 2 and "第2響" in bursts[0] and "第3響" in bursts[1]
    r = build_defense_reminder(50000.0, elapsed_h=6.2, nth=4)
    assert "第4則" in r and "50,000" in r and "預設不防守" in r
    c = build_defense_window_close(50000.0)
    assert "決策窗關閉" in c and "不防守" in c


# ── fallback：防守憑證未設 → 走日常通道 ───────────────────────────

def test_defense_send_falls_back_to_default_channel(monkeypatch):
    import service.notification.core as core
    calls = []
    monkeypatch.setattr(core, "DEFENSE_LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setattr(core, "DEFENSE_LINE_USER_ID", "")
    monkeypatch.setattr(core, "_send_line_message", lambda msgs: calls.append(("default", msgs)) or True)
    assert core._send_defense_line_message([{"type": "text", "text": "x"}]) is True
    assert calls and calls[0][0] == "default"


def test_defense_send_uses_dedicated_channel_when_configured(monkeypatch):
    import service.notification.core as core
    calls = []
    monkeypatch.setattr(core, "DEFENSE_LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(core, "DEFENSE_LINE_USER_ID", "uid")
    monkeypatch.setattr(core, "_push_line",
                        lambda tok, uid, msgs, label="x": calls.append((tok, uid, label)) or True)
    monkeypatch.setattr(core, "_send_line_message",
                        lambda msgs: (_ for _ in ()).throw(AssertionError("不應走日常通道")))
    assert core._send_defense_line_message([{"type": "text", "text": "x"}]) is True
    assert calls == [("tok", "uid", "Defense LINE")]


# ── 狀態機：觸發 → 提醒 → 窗關閉 → rearm 重置 ─────────────────────

@pytest.fixture
def patched(pa, tmp_path, monkeypatch):
    """state 檔導到 tmp，攔截三個推播出口與時間。"""
    state_file = tmp_path / "alert_state.json"
    monkeypatch.setattr(pa, "STATE_FILE", str(state_file))
    sent = {"full": [], "reminder": [], "close": []}
    monkeypatch.setattr(pa, "notify_defense_line", lambda p: sent["full"].append(p) or {})
    monkeypatch.setattr(pa, "notify_defense_reminder",
                        lambda p, h, n: sent["reminder"].append((p, round(h, 1), n)) or True)
    monkeypatch.setattr(pa, "notify_defense_window_close", lambda p: sent["close"].append(p) or True)
    t0 = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)
    clock = {"now": t0}
    monkeypatch.setattr(pa, "_now", lambda: clock["now"])
    return pa, state_file, sent, clock, t0


def _run_at(pa, monkeypatch, price, clock, when):
    clock["now"] = when
    monkeypatch.setattr(pa, "fetch_btc_price", lambda: price)
    pa.main()


def test_defense_event_lifecycle(patched, monkeypatch):
    pa, state_file, sent, clock, t0 = patched
    low = pa.ALERT_PRICE_LOW

    # (1) 跌破 → 全量警報＋開窗
    _run_at(pa, monkeypatch, low - 100, clock, t0)
    assert sent["full"] == [low - 100]
    st = json.loads(state_file.read_text())
    assert st["armed_defense"] is False and st["defense_reminder_idx"] == 0

    # (2) +1.5h 仍低於 → 第 1 則提醒
    _run_at(pa, monkeypatch, low - 200, clock, t0 + timedelta(hours=1.5))
    assert len(sent["reminder"]) == 1 and sent["reminder"][0][2] == 1

    # (3) +1.7h（同里程碑內）→ 不重複
    _run_at(pa, monkeypatch, low - 150, clock, t0 + timedelta(hours=1.7))
    assert len(sent["reminder"]) == 1

    # (4) +7h（停擺補跑）→ 只補最新一則（idx 跳 4）
    _run_at(pa, monkeypatch, low - 300, clock, t0 + timedelta(hours=7))
    assert len(sent["reminder"]) == 2 and sent["reminder"][1][2] == 4

    # (5) +25h → 窗關閉（預設不防守），只推一次
    _run_at(pa, monkeypatch, low - 300, clock, t0 + timedelta(hours=25))
    assert sent["close"] == [low - 300]
    _run_at(pa, monkeypatch, low - 300, clock, t0 + timedelta(hours=26))
    assert len(sent["close"]) == 1 and len(sent["full"]) == 1   # 靜默

    # (6) 回升 rearm → 事件狀態全清
    _run_at(pa, monkeypatch, low + pa.ALERT_PRICE_REARM_GAP + 1, clock, t0 + timedelta(hours=30))
    st = json.loads(state_file.read_text())
    assert st["armed_defense"] is True and "defense_event_start" not in st

    # (7) 次日再跌破 → 重新全量警報（新事件）
    _run_at(pa, monkeypatch, low - 50, clock, t0 + timedelta(hours=31))
    assert len(sent["full"]) == 2


def test_legacy_state_without_event_keys_is_safe(patched, monkeypatch):
    """舊 state 檔（無 U5-① 新鍵、armed=False）：不炸、不推，維持靜默至 rearm。"""
    pa, state_file, sent, clock, t0 = patched
    state_file.write_text(json.dumps(
        {"last_defense_date": "2026-07-13", "armed_defense": False}))
    _run_at(pa, monkeypatch, pa.ALERT_PRICE_LOW - 100, clock, t0)
    assert sent["full"] == [] and sent["reminder"] == [] and sent["close"] == []
