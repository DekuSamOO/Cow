"""哨兵判定口徑＝收完日線、狀態鏈守門補齊（2026-11-20，余力 D 紅隊 D1 No.1／No.2、D2 No.1／No.2）。

- D1：升槓桿 AHR999 與 D3 c1／c3 舊版吃盤中值（fetch_market_data 縫上的今日 K 棒／Coinbase 現價），
  研究與 PREREG 都是「收盤」。改吃 `closed_daily_last`。
- D2 No.1：升槓桿哨兵沒接 `_state_chain_broken`，舊 state 會重推已投批次。
- D2 No.2：artifact 存在但下載失敗＝空 state，舊版當成「第一次跑」放行，會重推「[開啟] 第 1/6 批」。
"""
import os
import sys
import json
import importlib.util
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.sentinel_board import closed_daily_last  # noqa: E402


def _load_notify():
    spec = importlib.util.spec_from_file_location(
        "dln_closed_chain", os.path.join(_REPO, "scripts", "daily_line_notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def notify():
    return _load_notify()


@pytest.fixture
def env(notify, tmp_path, monkeypatch):
    state_file = tmp_path / "escape_alert_state.json"
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(state_file))
    monkeypatch.setattr(notify, "_LINE_SEND_FAILURES", [])
    monkeypatch.delenv("STATE_ARTIFACT_CREATED_AT", raising=False)
    monkeypatch.delenv("STATE_RESTORE_FAILED", raising=False)
    sent = []
    import service.notification.core as core
    monkeypatch.setattr(core, "_send_line_message", lambda msgs: sent.append(msgs[0]) or True)
    texts = lambda: [json.dumps(m, ensure_ascii=False) for m in sent]
    load = lambda: json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    return sent, texts, load, state_file


# ── closed_daily_last ───────────────────────────────────────────────────

def _daily(values):
    today = datetime.now(timezone.utc).date()
    idx = pd.DatetimeIndex([pd.Timestamp(today - timedelta(days=len(values) - 1 - i))
                            for i in range(len(values))])
    return pd.DataFrame({"close": values, "AHR999": values}, index=idx)


def test_closed_daily_last_excludes_today_bar():
    df = _daily([1.0, 2.0, 3.0])                  # 最後一列＝UTC 今天（盤中）
    v, d = closed_daily_last(df, "close")
    assert v == 2.0 and d == str(df.index[-2])[:10]


def test_closed_daily_last_missing_returns_none():
    assert closed_daily_last(_daily([1.0, np.nan, 3.0]), "close") == (None, None)
    assert closed_daily_last(_daily([5.0]), "close") == (None, None)   # 只有今天
    assert closed_daily_last(_daily([1.0, 2.0]), "nope") == (None, None)


# ── D1 No.1：D3 c1 用收完日線收盤，現價插針不得觸發 ─────────────────────

def _d3_data(judge_close, current=61_000.0):
    # 低點 40,000、120 天前；c1 需 ≥ 60,000；c3 需 ≤ 100,000×0.8＝80,000
    lo_date = str(datetime.now(timezone.utc).date() - timedelta(days=120))
    return {"bear_low_since_ath": 40_000.0, "bear_low_date": lo_date,
            "days_since_bear_low": 120, "cycle_ath": 100_000.0,
            "current_price": current, "close_closed": judge_close}


def test_d3_intraday_spike_does_not_confirm(notify, env, monkeypatch):
    sent, texts, load, _ = env
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    notify.maybe_send_bear_bottom_confirm_alert(_d3_data(judge_close=58_000.0))
    assert not any("熊底確認" in t and "D3 成立" in t for t in texts()), "現價插針不得推熊底確認"
    assert not load().get("d3_confirmed")


def test_d3_confirms_on_closed_close(notify, env, monkeypatch):
    sent, texts, load, _ = env
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    notify.maybe_send_bear_bottom_confirm_alert(_d3_data(judge_close=61_000.0, current=59_000.0))
    assert any("D3 成立" in t for t in texts()), "收盤已確認就要推（即使現價回落）"
    assert load().get("d3_confirmed") is True


def test_d3_missing_closed_close_is_data_gap(notify, env):
    sent, texts, load, _ = env
    notify.maybe_send_bear_bottom_confirm_alert(_d3_data(judge_close=None))
    assert any("哨兵失明" in t for t in texts())
    assert not load().get("d3_confirmed")


# ── D2：升槓桿接上狀態鏈守門 ──────────────────────────────────────────────

def _lev_open():
    from config import LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH
    return {"ahr999": LEVERAGE_AHR999_MAX - 0.05,
            "days_since_ath": LEVERAGE_MIN_DAYS_FROM_ATH + 10,
            "current_price": 50_000.0, "bear_low_since_ath": 48_000.0}


def test_leverage_blocked_on_stale_state(notify, env, monkeypatch):
    sent, texts, load, state_file = env
    state_file.write_text(json.dumps({"lev_window_open": False, "lev_batches_sent": 0}),
                          encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(hours=notify.STATE_STALE_HOURS + 10)).isoformat()
    monkeypatch.setenv("STATE_ARTIFACT_CREATED_AT", old)
    notify.maybe_send_leverage_window_alert(_lev_open())
    t = texts()
    assert any("狀態鏈斷裂" in x for x in t), "舊 state 必須告警"
    assert not any("[開啟] 升槓桿窗口" in x for x in t), "舊 state 上不得推開窗第 1 批"
    assert not load().get("lev_batches_sent"), "斷鏈時批次計數不得前進"


def test_leverage_blocked_on_restore_failure_with_empty_state(notify, env, monkeypatch):
    sent, texts, load, _ = env
    monkeypatch.setenv("STATE_RESTORE_FAILED", "1")
    notify.maybe_send_leverage_window_alert(_lev_open())
    t = texts()
    assert any("狀態鏈斷裂" in x and "空狀態" in x for x in t), "還原失敗必須告警，不可當第一次跑"
    assert not any("[開啟] 升槓桿窗口" in x for x in t)
    assert not load().get("lev_batches_sent")


def test_leverage_first_run_empty_state_still_opens(notify, env):
    """真的查無 artifact（第一次跑）＝合法空 state，照常開窗（不可被新守門誤擋）。"""
    sent, texts, load, _ = env
    notify.maybe_send_leverage_window_alert(_lev_open())
    assert any("[開啟] 升槓桿窗口" in x for x in texts())
    assert load().get("lev_batches_sent") == 1


# ── D2 No.9：收完日線落後 >1 天＝舊值，不判定 ───────────────────────────

def _ago(n):
    return str(datetime.now(timezone.utc).date() - timedelta(days=n))


def test_leverage_stale_closed_bar_is_data_gap(notify, env):
    sent, texts, load, _ = env
    notify.maybe_send_leverage_window_alert({**_lev_open(), "ahr999_closed_date": _ago(3)})
    t = texts()
    assert any("哨兵失明" in x and "落後 3 天" in x for x in t)
    assert not any("[開啟] 升槓桿窗口" in x for x in t)
    assert not load().get("lev_batches_sent")


def test_leverage_fresh_closed_bar_opens(notify, env):
    sent, texts, load, _ = env
    notify.maybe_send_leverage_window_alert({**_lev_open(), "ahr999_closed_date": _ago(1)})
    assert any("[開啟] 升槓桿窗口" in x for x in texts())


def test_d3_stale_closed_bar_is_data_gap(notify, env, monkeypatch):
    sent, texts, load, _ = env
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    notify.maybe_send_bear_bottom_confirm_alert({**_d3_data(61_000.0), "close_closed_date": _ago(4)})
    assert any("哨兵失明" in x for x in texts())
    assert not load().get("d3_confirmed")


# ── D2 No.6：D3 已確認（情境二）→ 升槓桿不推批次 ──────────────────────────

def test_leverage_silent_after_d3_confirmed(notify, env):
    sent, texts, load, state_file = env
    state_file.write_text(json.dumps({"d3_confirmed": True}), encoding="utf-8")
    notify.maybe_send_leverage_window_alert(_lev_open())
    assert not any("升槓桿窗口" in x for x in texts())
    assert not load().get("lev_batches_sent")


# ── D2 No.7：D3 誤報重置 ─────────────────────────────────────────────────

def test_d3_confirm_records_low(notify, env, monkeypatch):
    sent, texts, load, _ = env
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    notify.maybe_send_bear_bottom_confirm_alert(_d3_data(judge_close=61_000.0))
    assert load().get("d3_confirmed_low") == 40_000.0


def test_d3_false_alarm_reset_when_close_breaks_confirmed_low(notify, env, monkeypatch):
    sent, texts, load, state_file = env
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    state_file.write_text(json.dumps({"d3_confirmed": True, "d3_confirmed_date": "2026-01-01",
                                      "d3_confirmed_low": 40_000.0}), encoding="utf-8")
    notify.maybe_send_bear_bottom_confirm_alert(_d3_data(judge_close=39_000.0))
    assert any("D3 誤報重置" in x for x in texts())
    st = load()
    assert "d3_confirmed" not in st and "d3_confirmed_low" not in st


def test_d3_no_reset_while_above_confirmed_low(notify, env, monkeypatch):
    sent, texts, load, state_file = env
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    state_file.write_text(json.dumps({"d3_confirmed": True, "d3_confirmed_low": 40_000.0}),
                          encoding="utf-8")
    notify.maybe_send_bear_bottom_confirm_alert(_d3_data(judge_close=41_000.0))
    assert not any("誤報重置" in x for x in texts())
    assert load().get("d3_confirmed") is True


def test_utc_day_count_helper(notify):
    assert notify._closed_lag_days(_ago(1)) == 1
    assert notify._closed_lag_days(None) is None
    assert abs((notify._utc_now_naive() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 5