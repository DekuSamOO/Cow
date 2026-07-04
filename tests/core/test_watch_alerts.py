"""core/watch_alerts.py 測試（E2 警戒引擎）。價格序列模擬：觸發一次、防抖、重武裝。"""
import datetime
import sys

sys.path.insert(0, ".")
from core.watch_alerts import check_price_events, check_signal_change  # noqa: E402
from core.watch_plan import _parse_one  # noqa: E402

_PLAN_LONG = _parse_one("2330", {"direction": "long", "entry": [950, 970], "stop": 920,
                                 "targets": [1050, 1120]})
_PLAN_SHORT = _parse_one("QQQ", {"direction": "short", "entry": [740, 745], "stop": 760,
                                 "targets": [700]})


def _run_seq(plan, prices):
    """依序餵價格，回傳每步的事件種類清單。"""
    state, out = {}, []
    for p in prices:
        evs, state = check_price_events(plan, p, state)
        out.append([e["event"] for e in evs])
    return out, state


# ── 進場區：入區響一次，區內震盪不重複，出區超過 gap 後再入區才再響 ──────────

def test_entry_fires_once_then_rearms_after_leaving_zone():
    out, _ = _run_seq(_PLAN_LONG, [
        1000,   # 區外（上方）→ 無
        965,    # 入區 → entry
        955,    # 區內震盪 → 不重複
        971,    # 出區但 < 970*1.005=974.85 → 仍未武裝
        976,    # 出區超過 gap → 重新武裝（無事件）
        960,    # 再入區 → entry 再響
    ])
    assert out == [[], ["entry"], [], [], [], ["entry"]]


# ── 停損：跌破響一次，門檻下方震盪不狂響，回升超過 gap 才重武裝 ─────────────

def test_stop_hysteresis_no_spam():
    out, _ = _run_seq(_PLAN_LONG, [
        930,            # 未破 → 無
        919,            # 破停損 → stop（同時 919 < 950*0.995=945.25... 未入過區，entry 仍武裝但 919 不在區內）
        918, 920, 917,  # 停損下方震盪 → 不重複
        923,            # 回升但 < 920*1.005=924.6 → 未重武裝
        925,            # 超過 gap → 重武裝
        919,            # 再破 → stop 再響
    ])
    assert out == [[], ["stop"], [], [], [], [], [], ["stop"]]


# ── 目標：各目標獨立武裝，T1 響過後 T2 到價仍會響 ──────────────────────────

def test_targets_independent():
    out, _ = _run_seq(_PLAN_LONG, [1055, 1060, 1125])
    assert out == [["target_1"], [], ["target_2"]]


# ── 空單方向鏡像：停損在上方 ────────────────────────────────────────────────

def test_short_stop_above():
    out, _ = _run_seq(_PLAN_SHORT, [750, 761, 758, 699])
    assert out == [[], ["stop"], [], ["target_1"]]


# ── 過期計畫與無效價不觸發 ──────────────────────────────────────────────────

def test_expired_plan_silent():
    p = _parse_one("2330", {"direction": "long", "entry": [950, 970], "stop": 920,
                            "targets": [1050], "valid_until": "2026-01-01"})
    evs, _ = check_price_events(p, 960, {}, today=datetime.date(2026, 7, 4))
    assert evs == []


def test_invalid_price_silent():
    for bad in (None, 0, -1):
        evs, _ = check_price_events(_PLAN_LONG, bad, {})
        assert evs == []


# ── 訊號變化：首次觀測不響、變化響、同 key 去重 ─────────────────────────────

def test_signal_change_dedup():
    evs, st = check_signal_change("2330", "RIDE", "順勢持有", {})
    assert evs == []                                    # 首次觀測非「變化」
    evs, st = check_signal_change("2330", "RIDE", "順勢持有", st)
    assert evs == []                                    # 同 key 不重複
    evs, st = check_signal_change("2330", "TAKE_PROFIT", "分批止盈", st)
    assert len(evs) == 1 and "順勢持有 → 分批止盈" in evs[0]["msg"]
    evs, _ = check_signal_change("2330", "TAKE_PROFIT", "分批止盈", st)
    assert evs == []
