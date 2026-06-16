"""逃頂警報分級/去重/遲滯 與 分數 Δ 的狀態機測試（不實際發送 LINE）。"""
import sys
import os
import json
import importlib.util
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from service.notification.builders import escape_alert_tier


def _load_notify_module():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "daily_line_notify", os.path.join(repo, "scripts", "daily_line_notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def notify():
    return _load_notify_module()


def test_escape_alert_tier_mapping():
    assert escape_alert_tier(59) == (0, None)
    assert escape_alert_tier(60) == (60, "預警")
    assert escape_alert_tier(74) == (60, "預警")
    assert escape_alert_tier(75) == (75, "警報")
    assert escape_alert_tier(85) == (85, "危急")
    assert escape_alert_tier(100) == (85, "危急")


@pytest.fixture
def patched_state(notify, tmp_path, monkeypatch):
    """state 檔導到 tmp，攔截 LINE 發送，凍結 builders 不依賴完整 data。"""
    state_file = tmp_path / "escape_alert_state.json"
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(state_file))
    sent = []
    import service.notification.core as core
    monkeypatch.setattr(core, "_send_line_message", lambda msgs: sent.append(msgs))
    import service.notification.builders as builders
    monkeypatch.setattr(builders, "build_escape_alert_flex", lambda s: {"type": "flex"})
    return state_file, sent


def _data(score):
    return {"escape_score": score, "escape_signals": {"derivatives": {"score": 1, "max": 30}}}


def test_escape_alert_below_threshold_no_push(notify, patched_state):
    state_file, sent = patched_state
    notify.maybe_send_escape_alert(_data(40))
    assert sent == []
    assert not state_file.exists() or "last_escape_score" not in json.loads(state_file.read_text())


def test_escape_alert_first_cross_pushes(notify, patched_state):
    state_file, sent = patched_state
    notify.maybe_send_escape_alert(_data(62))
    assert len(sent) == 1
    st = json.loads(state_file.read_text())
    assert st["last_escape_score"] == 62 and st["last_escape_tier"] == 60


def test_escape_alert_same_day_dedupe(notify, patched_state):
    _, sent = patched_state
    notify.maybe_send_escape_alert(_data(62))
    notify.maybe_send_escape_alert(_data(70))  # 同日即使 +8 也不再推
    assert len(sent) == 1


def test_escape_alert_cross_day_needs_delta_or_upgrade(notify, patched_state):
    state_file, sent = patched_state
    # 模擬「昨日已以 62 分推播過」
    state_file.write_text(json.dumps(
        {"last_escape_date": "2020-01-01", "last_escape_score": 62, "last_escape_tier": 60}))
    notify.maybe_send_escape_alert(_data(64))  # +2 < 5 且未升級 → 不推
    assert len(sent) == 0
    notify.maybe_send_escape_alert(_data(67))  # +5 → 推
    assert len(sent) == 1
    # 再模擬昨日 74 分 → 今日 76 雖只 +2 但升級「警報」→ 推
    state_file.write_text(json.dumps(
        {"last_escape_date": "2020-01-01", "last_escape_score": 74, "last_escape_tier": 60}))
    notify.maybe_send_escape_alert(_data(76))
    assert len(sent) == 2


def test_escape_alert_disarm_on_drop(notify, patched_state):
    state_file, sent = patched_state
    state_file.write_text(json.dumps(
        {"last_escape_date": "2020-01-01", "last_escape_score": 70, "last_escape_tier": 60}))
    notify.maybe_send_escape_alert(_data(30))  # 跌回門檻下 → 解除武裝
    st = json.loads(state_file.read_text())
    assert "last_escape_score" not in st
    notify.maybe_send_escape_alert(_data(61))  # 重新跨門檻 → 新事件，直接推
    assert len(sent) == 1


def test_attach_score_deltas(notify, patched_state):
    state_file, _ = patched_state
    # 前一日分數 50/40 → 今日 62/35 → Δ +12/-5
    state_file.write_text(json.dumps(
        {"score_history": {"2020-01-01": {"escape": 50, "low": 40}}}))
    data = {"escape_score": 62, "low_score": 35}
    notify.attach_score_deltas(data)
    assert data["escape_delta"] == 12
    assert data["low_delta"] == -5
    hist = json.loads(state_file.read_text())["score_history"]
    assert any(v == {"escape": 62, "low": 35} for k, v in hist.items() if k != "2020-01-01")


def test_attach_score_deltas_first_run_no_delta(notify, patched_state):
    data = {"escape_score": 62, "low_score": 35}
    notify.attach_score_deltas(data)
    assert "escape_delta" not in data and "low_delta" not in data


# ── 週報 ──────────────────────────────────────────────────────────────────────
_TW = timezone(timedelta(hours=8))
_SUN_EVE = datetime(2026, 6, 14, 18, 30, tzinfo=_TW)   # 週日 18:30


def _weekly_data():
    return {"price": "$100,000", "week_change_pct": 3.2, "week_high": 105000.0,
            "week_low": 98000.0, "trend_level": "🟢 多頭趨勢",
            "composite_action": "順勢持有", "composite_pos": "建議倉位 60–80%（未擬合）"}


def _flex_text(msg):
    """攤平 Flex 訊息所有 text 欄位 + altText 成單一字串，供子字串斷言。"""
    out = [msg.get("altText", "")]

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "text" and "text" in o:
                out.append(o["text"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(msg)
    return " ".join(out)


def test_weekly_summary_only_sunday_evening(notify, patched_state):
    _, sent = patched_state
    notify.maybe_send_weekly_summary(_weekly_data(), now=datetime(2026, 6, 10, 18, 30, tzinfo=_TW))  # 週三
    notify.maybe_send_weekly_summary(_weekly_data(), now=datetime(2026, 6, 14, 8, 30, tzinfo=_TW))   # 週日早上
    assert sent == []
    notify.maybe_send_weekly_summary(_weekly_data(), now=_SUN_EVE)
    assert len(sent) == 1
    msg = sent[0][0]
    assert msg["type"] == "flex"          # 已改 Flex Message（非純文字）
    text = _flex_text(msg)
    assert "BTC 週報" in text and "+3.2%" in text and "今日行動" not in text


def test_weekly_summary_dedupe_and_scores(notify, patched_state):
    state_file, sent = patched_state
    import json as _json
    state_file.write_text(_json.dumps({"score_history": {
        "2026-06-12": {"escape": 30, "low": 50},
        "2026-06-13": {"escape": 45, "low": 40},
        "2026-06-14": {"escape": 38, "low": 42},
    }}))
    notify.maybe_send_weekly_summary(_weekly_data(), now=_SUN_EVE)
    assert len(sent) == 1
    msg = sent[0][0]
    assert msg["type"] == "flex"
    text = _flex_text(msg)
    # 逃頂分週高/低（max 45 / min 30）與抄底分週高/低（max 50 / min 40）入卡
    assert "逃頂分" in text and "45" in text and "30" in text
    assert "抄底分" in text and "50" in text
    # 同日再呼叫 → 去重
    notify.maybe_send_weekly_summary(_weekly_data(), now=_SUN_EVE)
    assert len(sent) == 1


def test_weekly_summary_insufficient_data_skipped(notify, patched_state):
    _, sent = patched_state
    notify.maybe_send_weekly_summary({}, now=_SUN_EVE)   # 無價格也無分數史
    assert sent == []
