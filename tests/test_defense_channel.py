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
    monkeypatch.setattr(pa, "_SEND_FAILURES", [])
    monkeypatch.setattr(pa, "SEND_FAILURE_MARKER", str(tmp_path / "line_send_failures.txt"))
    # 回傳形狀同 facade.notify_defense_line；首響以 'line' 判定送達（2026-11-20 起會看回傳值）
    monkeypatch.setattr(pa, "notify_defense_line",
                        lambda p: sent["full"].append(p) or {"line": True})
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


# ── 防守階梯到價推播（2026-11-20 立）：首響後下探到新一階才推，每階每事件一次 ──
# 數字一律顯假值（公開檔禁真值，CLAUDE.md 已知陷阱 21）

_FAKE_LOW = 99_999.0
_FAKE_LADDER = (
    (88_888.0, "範例動作A", 0.1, 11_111.0, "附註A"),
    (77_777.0, "範例動作B", 0.2, 10_000.0, ""),
    (66_666.0, "範例動作C", 0.3, 9_999.0, "附註C"),
)


@pytest.fixture
def tiered(patched, monkeypatch):
    pa, state_file, sent, clock, t0 = patched
    monkeypatch.setattr(pa, "ALERT_PRICE_LOW", _FAKE_LOW)
    monkeypatch.setattr(pa, "DEFENSE_LADDER", _FAKE_LADDER)
    sent["tier"] = []
    monkeypatch.setattr(pa, "notify_defense_tier",
                        lambda p, hits: sent["tier"].append((p, [i for i, _ in hits])) or True)
    return pa, state_file, sent, clock, t0


def test_tier_push_lifecycle(tiered, monkeypatch):
    pa, state_file, sent, clock, t0 = tiered
    h = lambda x: t0 + timedelta(hours=x)

    _run_at(pa, monkeypatch, 95_000.0, clock, h(0))          # 首響，未觸及任何階
    assert len(sent["full"]) == 1 and sent["tier"] == []
    _run_at(pa, monkeypatch, 88_000.0, clock, h(1.2))        # 下探到第 1 階
    assert sent["tier"] == [(88_000.0, [1])]
    _run_at(pa, monkeypatch, 87_000.0, clock, h(2.2))        # 同一階不重推
    assert len(sent["tier"]) == 1
    _run_at(pa, monkeypatch, 60_000.0, clock, h(30))         # 窗已關仍推；一次跳兩階合併一則
    assert sent["tier"][-1] == (60_000.0, [2, 3])
    assert sent["close"], "決策窗應已關閉"
    _run_at(pa, monkeypatch, 59_000.0, clock, h(31))
    assert len(sent["tier"]) == 2

    # 回升 rearm → 清空；新事件首響時已在第 1 階下 → 算已通知，不另推
    _run_at(pa, monkeypatch, _FAKE_LOW + pa.ALERT_PRICE_REARM_GAP + 1, clock, h(40))
    assert "defense_tiers_notified" not in json.loads(state_file.read_text())
    _run_at(pa, monkeypatch, 80_000.0, clock, h(50))
    assert len(sent["full"]) == 2 and len(sent["tier"]) == 2
    _run_at(pa, monkeypatch, 77_000.0, clock, h(51))         # 新事件下探到第 2 階
    assert sent["tier"][-1] == (77_000.0, [2])


def test_tier_push_failure_retries(tiered, monkeypatch):
    pa, state_file, sent, clock, t0 = tiered
    _run_at(pa, monkeypatch, 95_000.0, clock, t0)
    monkeypatch.setattr(pa, "notify_defense_tier", lambda p, hits: False)
    _run_at(pa, monkeypatch, 88_000.0, clock, t0 + timedelta(hours=1.2))
    assert json.loads(state_file.read_text()).get("defense_tiers_notified") == []
    monkeypatch.setattr(pa, "notify_defense_tier",
                        lambda p, hits: sent["tier"].append((p, [i for i, _ in hits])) or True)
    _run_at(pa, monkeypatch, 88_000.0, clock, t0 + timedelta(hours=1.5))
    assert sent["tier"] == [(88_000.0, [1])]


def test_tier_alert_text():
    from service.notification.facade import build_defense_tier_alert
    t = build_defense_tier_alert(76_000.0, [(2, _FAKE_LADDER[1]), (3, _FAKE_LADDER[2])])
    assert "76,000" in t and "第2階 $77,777" in t and "第3階 $66,666" in t
    assert "範例動作B" in t and "~$9,999" in t and "⚠ 附註C" in t
    assert "第1階" not in t


# ── PREREG 事前保險絲（2026-11-20 立；顯假值）──

def test_prereg_fuse_once_per_event(tiered, monkeypatch):
    pa, state_file, sent, clock, t0 = tiered
    monkeypatch.setattr(pa, "PREREG_PESSIMISTIC_FLOOR", 33_333.0)
    fuse = []
    monkeypatch.setattr(pa, "notify_prereg_fuse", lambda p: fuse.append(p) or True)
    h = lambda x: t0 + timedelta(hours=x)

    _run_at(pa, monkeypatch, 40_000.0, clock, h(0))      # 跌破警報，尚未破保險絲
    assert fuse == []
    _run_at(pa, monkeypatch, 33_000.0, clock, h(1))      # 跌破保險絲：推一次
    _run_at(pa, monkeypatch, 30_000.0, clock, h(2))
    assert fuse == [33_000.0]
    _run_at(pa, monkeypatch, _FAKE_LOW + pa.ALERT_PRICE_REARM_GAP + 1, clock, h(10))
    assert "prereg_fuse_notified" not in json.loads(state_file.read_text())
    _run_at(pa, monkeypatch, 32_000.0, clock, h(20))     # 新事件再跌破：再推
    assert fuse == [33_000.0, 32_000.0]


def test_prereg_fuse_text():
    from service.notification.facade import build_prereg_fuse_alert
    t = build_prereg_fuse_alert(33_000.0, 33_333.0)
    assert "$33,000" in t and "$33,333" in t and "不防守" in t and "30%" in t


# ── 防守推播失敗不得前進 state（2026-11-20，余力 B 抓蟲 B5 No.1）──
# 舊版丟掉 facade 回傳值：首響 LINE 失敗仍解除武裝 → 全量防守訊息永久遺失且 exit 0。

def test_full_alert_failure_stays_armed_and_retries(patched, monkeypatch):
    pa, state_file, sent, clock, t0 = patched
    low = pa.ALERT_PRICE_LOW
    monkeypatch.setattr(pa, "notify_defense_line",
                        lambda p: sent["full"].append(p) or {"line": False, "telegram": True})
    _run_at(pa, monkeypatch, low - 100, clock, t0)
    st = json.loads(state_file.read_text())
    assert st.get("armed_defense", True) is True, "首響沒送到 LINE 卻解除武裝＝全量防守訊息永久遺失"
    assert "defense_event_start" not in st and not st.get("last_defense_date")
    monkeypatch.setattr(pa, "notify_defense_line",
                        lambda p: sent["full"].append(p) or {"line": True})
    _run_at(pa, monkeypatch, low - 100, clock, t0 + timedelta(hours=1))
    assert len(sent["full"]) == 2, "LINE 恢復後下一小時必須重送全量警報"
    assert json.loads(state_file.read_text())["armed_defense"] is False


def test_reminder_and_close_failure_do_not_advance(patched, monkeypatch):
    pa, state_file, sent, clock, t0 = patched
    low = pa.ALERT_PRICE_LOW
    _run_at(pa, monkeypatch, low - 100, clock, t0)                       # 首響成功
    monkeypatch.setattr(pa, "notify_defense_reminder", lambda p, h, n: False)
    _run_at(pa, monkeypatch, low - 100, clock, t0 + timedelta(hours=1.5))
    assert json.loads(state_file.read_text())["defense_reminder_idx"] == 0, "提醒沒送到卻前進 idx"
    monkeypatch.setattr(pa, "notify_defense_window_close", lambda p: False)
    _run_at(pa, monkeypatch, low - 100, clock, t0 + timedelta(hours=25))
    assert not json.loads(state_file.read_text()).get("defense_window_closed"), "窗關閉沒送到卻記為已關"
    monkeypatch.setattr(pa, "notify_defense_window_close", lambda p: sent["close"].append(p) or True)
    _run_at(pa, monkeypatch, low - 100, clock, t0 + timedelta(hours=26))
    assert sent["close"] == [low - 100], "LINE 恢復後必須補推窗關閉"


def test_send_failure_marker_only_on_actions(patched, monkeypatch, tmp_path):
    pa, state_file, sent, clock, t0 = patched
    monkeypatch.setattr(pa, "notify_defense_line", lambda p: {"line": False})
    marker = tmp_path / "line_send_failures.txt"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _run_at(pa, monkeypatch, pa.ALERT_PRICE_LOW - 100, clock, t0)
    assert not marker.exists(), "本機跑不該在 repo 留標記檔"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _run_at(pa, monkeypatch, pa.ALERT_PRICE_LOW - 100, clock, t0 + timedelta(hours=1))
    assert marker.exists() and "防守首響" in marker.read_text(encoding="utf-8")


def test_no_marker_when_all_sent(patched, monkeypatch, tmp_path):
    pa, state_file, sent, clock, t0 = patched
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _run_at(pa, monkeypatch, pa.ALERT_PRICE_LOW - 100, clock, t0)
    assert not (tmp_path / "line_send_failures.txt").exists()