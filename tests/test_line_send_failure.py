"""LINE 推送失敗時「只推一次」的哨兵不得前進 state（2026-11-20 立）。

立規原因＝余力 B 抓蟲 B5 No.5：底層 `_send_line_message` 失敗只回 False、不拋例外，
`daily_line_notify.send_line_message` 又丟掉回傳值，於是本檔所有
`try: send_line_message(...) except: return  # 不前進 state` 的守門都是死碼——
LINE token 過期或月額度用完（429）那場，升槓桿第 N 批／D3 成立等旗標照樣寫成已推，
使用者沒收到，之後也不會再推。

既有測試的假 send 一律「回 None 不拋例外」，所以這條落差從沒被測出來；
這裡改成讓**最底層**回 False（真實失敗的長相），從上層觀察 state。
"""
import sys
import os
import json
import importlib.util

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import pytest


def _load_notify_module():
    spec = importlib.util.spec_from_file_location(
        "dln_send_failure", os.path.join(_REPO, "scripts", "daily_line_notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def notify():
    return _load_notify_module()


@pytest.fixture
def line(notify, tmp_path, monkeypatch):
    """state 檔導到 tmp；底層 LINE 發送可切換成功／失敗，並記錄每次嘗試。"""
    state_file = tmp_path / "escape_alert_state.json"
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(state_file))
    monkeypatch.setattr(notify, "_LINE_SEND_FAILURES", [])
    monkeypatch.setattr(notify, "LINE_SEND_FAILURE_MARKER", str(tmp_path / "line_send_failures.txt"))
    monkeypatch.delenv("CRON_SCHEDULE", raising=False)
    ctl = {"ok": False, "tries": []}
    import service.notification.core as core
    monkeypatch.setattr(core, "_send_line_message",
                        lambda msgs: ctl["tries"].append(msgs[0].get("type")) or ctl["ok"])
    ctl["state"] = lambda: (json.loads(state_file.read_text(encoding="utf-8"))
                            if state_file.exists() else {})
    return ctl


# ── 出口本身 ──────────────────────────────────────────────────────────────

def test_send_raises_when_flex_and_text_both_fail(notify, line):
    with pytest.raises(notify.LineSendError):
        notify.send_line_message({"type": "text", "text": "a\nb"})
    assert line["tries"] == ["flex", "text"], "Flex 失敗仍要退回純文字再試一次"


def test_send_raises_for_flex_payload(notify, line):
    with pytest.raises(notify.LineSendError):
        notify.send_line_message({"type": "flex", "altText": "a", "contents": {}})


def test_send_returns_true_on_success(notify, line):
    line["ok"] = True
    assert notify.send_line_message({"type": "text", "text": "a"}) is True


# ── 只推一次的哨兵：失敗不前進、恢復後補推 ───────────────────────────────

def test_data_gap_alert_not_flagged_when_send_fails(notify, line):
    notify._maybe_send_data_gap_alert("leverage", "測試")
    assert not line["state"]().get("datagap_leverage"), "沒送出卻記成已推＝之後永遠不再提醒"
    line["ok"] = True
    line["tries"].clear()
    notify._maybe_send_data_gap_alert("leverage", "測試")
    assert line["tries"], "LINE 恢復後下一場必須重送"
    assert line["state"]().get("datagap_leverage") is True


def _open_window_data(notify):
    from config import LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH
    return {"ahr999": LEVERAGE_AHR999_MAX - 0.05,
            "days_since_ath": LEVERAGE_MIN_DAYS_FROM_ATH + 10,
            "current_price": 50_000.0,
            "bear_low_since_ath": 48_000.0}


def test_leverage_first_batch_not_counted_when_send_fails(notify, line):
    """窗口開啟那場 LINE 失敗 → 不得把第 1 批記成已發（舊版會前進為 1、使用者沒收到）。"""
    notify.maybe_send_leverage_window_alert(_open_window_data(notify))
    assert line["tries"], "前提：本場確實嘗試推送開窗訊息"
    st = line["state"]()
    assert not st.get("lev_batches_sent"), "推送失敗卻記成已投第 1 批"
    line["ok"] = True
    line["tries"].clear()
    notify.maybe_send_leverage_window_alert(_open_window_data(notify))
    assert line["tries"], "LINE 恢復後必須重推第 1 批"
    assert line["state"]().get("lev_batches_sent") == 1


def test_escape_alert_dedupe_not_written_when_send_fails(notify, line, monkeypatch):
    from config import ESCAPE_ALERT_TIERS
    import service.notification.builders as builders
    monkeypatch.setattr(builders, "build_escape_alert_flex",
                        lambda d: {"type": "flex", "altText": "x", "contents": {}})
    top = max(f for f, _ in ESCAPE_ALERT_TIERS) + 1
    notify.maybe_send_escape_alert({"escape_score": top})
    assert line["tries"], "前提：本場確實嘗試推送逃頂警報"
    assert line["state"]().get("last_escape_date") is None, "沒送出卻寫了今日已推，當天不會再試"


def test_action_flip_not_recorded_when_send_fails(notify, line, monkeypatch):
    import service.notification.builders as builders
    monkeypatch.setattr(builders, "build_action_alert_flex",
                        lambda d, prev: {"type": "flex", "altText": "x", "contents": {}})
    notify._save_escape_state({"last_action_key": "old", "last_action_label": "舊"})
    notify.maybe_send_action_alert({"composite_key": "new", "composite_action": "新"})
    assert line["tries"], "前提：本場確實嘗試推送行動翻轉"
    assert line["state"]().get("last_action_key") == "old", "沒送出卻前進 key，翻轉訊息就此遺失"


# ── 失敗標記檔：workflow 在上傳 state 之後據此判 job 失敗（2026-11-20 甲案）──────

def test_marker_written_on_actions_when_send_fails(notify, line, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    notify._maybe_send_data_gap_alert("d3", "測試")
    notify._write_send_failure_marker()
    marker = tmp_path / "line_send_failures.txt"
    assert marker.exists(), "推送失敗卻沒留標記＝workflow 判不到失敗、沒人知道"
    assert "哨兵失明" in marker.read_text(encoding="utf-8")


def test_no_marker_when_all_sent(notify, line, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    line["ok"] = True
    notify._maybe_send_data_gap_alert("d3", "測試")
    notify._write_send_failure_marker()
    assert not (tmp_path / "line_send_failures.txt").exists()


def test_no_marker_outside_actions(notify, line, monkeypatch, tmp_path):
    """本機跑時對外閘門擋下也算失敗，但不該在 repo 根目錄留檔。"""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    notify._maybe_send_data_gap_alert("d3", "測試")
    notify._write_send_failure_marker()
    assert not (tmp_path / "line_send_failures.txt").exists()


def test_weekly_text_fallback_success_is_not_a_failure(notify, line, monkeypatch, tmp_path):
    """週報 Flex 被拒、文字版送到 → 使用者有收到，不得讓 job 判失敗。"""
    from datetime import datetime, timezone, timedelta
    import service.notification.core as core
    tries = []
    monkeypatch.setattr(core, "_send_line_message",
                        lambda msgs: tries.append(msgs[0].get("type")) or msgs[0].get("type") == "text")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    sun_eve = datetime(2026, 6, 14, 18, 30, tzinfo=timezone(timedelta(hours=8)))
    notify.maybe_send_weekly_summary(
        {"price": "$100,000", "week_change_pct": 3.2, "week_high": 105000.0,
         "week_low": 98000.0, "trend_level": "多頭", "composite_action": "持有",
         "composite_pos": "60%"}, now=sun_eve)
    assert tries and tries[-1] == "text", "前提：最後是文字版送出"
    assert line["state"]().get("last_weekly_date") == "2026-06-14"
    notify._write_send_failure_marker()
    assert not (tmp_path / "line_send_failures.txt").exists()