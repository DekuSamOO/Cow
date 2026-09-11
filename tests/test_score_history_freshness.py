# -*- coding: utf-8 -*-
"""
score_history 的「日期新鮮度」與第 1 批首推日校正 —— 2026-09-11 立。

**為什麼要有這支**：狀態鏈斷裂那 9 天（09-01~09-09，根因與修法見 vault
`Github/Cow/歷程/20260911fix_哨兵狀態鏈斷裂.md`）留下的 state 長這樣：

    score_history = 08-25 … 08-31（7 筆）＋ 09-10（1 筆）  ← 8 筆，橫跨 17 天

筆數是滿的，所以畫面上完全看不出中間缺 9 天。舊碼兩處都用「筆數」思考：
  · Δ 取「最近的前一筆」→ 09-10 那天實際拿 08-31 當基準，卻標成「vs 昨日」；
  · 週報把全部 8 筆當「本週」→ 在橫跨兩週的拼接資料上算週高/週低。

所以本檔測的紅線是：**這兩處一律比日期，不比筆數**。
另加第 1 批首推日的一次性校正（被重推洗成 09-10，真值 09-08）。
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_NOTIFY_PATH = os.path.join(_REPO, "scripts", "daily_line_notify.py")
_TW = timezone(timedelta(hours=8))


@pytest.fixture(scope="module")
def notify():
    spec = importlib.util.spec_from_file_location("daily_line_notify", _NOTIFY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def state_file(notify, tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(p))
    return p


def _write(state_file, payload):
    state_file.write_text(json.dumps(payload), encoding="utf-8")


def _read(state_file):
    return json.loads(state_file.read_text(encoding="utf-8"))


def _iso(days_ago, today):
    return str(today - timedelta(days=days_ago))


# ── Δ 基準的新鮮度 ──────────────────────────────────────────────────────────

def test_delta_uses_recent_baseline(notify, state_file):
    """基準在容忍範圍內（昨天）→ 照常給 Δ。"""
    today = notify.date.today()
    _write(state_file, {"score_history": {_iso(1, today): {"escape": 10, "low": 30}}})
    data = {"escape_score": 17, "low_score": 31}
    notify.attach_score_deltas(data)
    assert data["escape_delta"] == 7
    assert data["low_delta"] == 1


def test_delta_suppressed_when_baseline_is_stale(notify, state_file):
    """
    基準是 10 天前（正是斷鏈後的實況）→ **不得給 Δ**。
    給了就會在畫面上把 10 天前的分數標成「vs 昨日」，那是編造出來的變化量。
    """
    today = notify.date.today()
    _write(state_file, {"score_history": {_iso(10, today): {"escape": 3, "low": 29}}})
    data = {"escape_score": 17, "low_score": 31}
    notify.attach_score_deltas(data)
    assert "escape_delta" not in data
    assert "low_delta" not in data


def test_delta_boundary_is_inclusive(notify, state_file):
    """剛好等於容忍上限仍算新鮮（容得下週末或單場漏跑）。"""
    today = notify.date.today()
    gap = notify.SCORE_DELTA_MAX_GAP_DAYS
    _write(state_file, {"score_history": {_iso(gap, today): {"escape": 10, "low": 30}}})
    data = {"escape_score": 12, "low_score": 30}
    notify.attach_score_deltas(data)
    assert data["escape_delta"] == 2


# ── 保留策略：按日期丟，不是只按筆數留 ────────────────────────────────────────

def test_stale_entries_are_dropped_by_date(notify, state_file):
    """
    重現線上那份 state：7 筆很舊 + 1 筆新。舊碼會原封不動留下 8 筆，
    修好之後超過保留天數的必須消失。
    """
    today = notify.date.today()
    hist = {_iso(d, today): {"escape": 5, "low": 29} for d in range(16, 23)}
    hist[_iso(1, today)] = {"escape": 17, "low": 31}
    _write(state_file, {"score_history": hist})

    notify.attach_score_deltas({"escape_score": 18, "low_score": 32})
    kept = sorted(_read(state_file)["score_history"])

    assert all(notify._date_gap_days(d, str(today)) <= notify.SCORE_HISTORY_KEEP_DAYS
               for d in kept), "保留天數外的舊分數不得留下"
    assert kept == [_iso(1, today), str(today)]


def test_recent_entries_still_capped_at_eight(notify, state_file):
    """新鮮但筆數過多時，原本的 8 筆上限仍然生效。"""
    today = notify.date.today()
    hist = {_iso(d, today): {"escape": d, "low": d} for d in range(1, 13)}
    _write(state_file, {"score_history": hist})
    notify.attach_score_deltas({"escape_score": 99, "low_score": 99})
    assert len(_read(state_file)["score_history"]) == 8


# ── 週報：「本週」用日期界定 ──────────────────────────────────────────────────

def _sunday_evening():
    d = datetime(2026, 9, 13, 18, 0, tzinfo=_TW)
    assert d.weekday() == 6, "本測試預設 2026-09-13 是週日"
    return d


def test_weekly_summary_ignores_out_of_week_scores(notify, state_file, monkeypatch):
    """
    餵入斷鏈後那種拼接資料：兩週前有極端值、本週只有溫和值。
    週報的「週高/週低」只能反映本週那幾筆，兩週前的極端值不得入列。
    """
    from service.notification import builders
    monkeypatch.setattr(builders, "build_weekly_flex", lambda *a, **k: None)
    box = []
    monkeypatch.setattr(notify, "send_line_message", lambda payload: box.append(payload))

    _write(state_file, {"score_history": {
        "2026-08-30": {"escape": 99, "low": 1},     # 兩週前的極端值
        "2026-08-31": {"escape": 98, "low": 2},
        "2026-09-11": {"escape": 17, "low": 31},    # 本週
        "2026-09-12": {"escape": 15, "low": 33},
        "2026-09-13": {"escape": 16, "low": 32},
    }})

    notify.maybe_send_weekly_summary({"trend_level": "測試"}, now=_sunday_evening())

    assert len(box) == 1
    text = box[0]["text"]
    assert "週高 17／週低 15" in text, text
    assert "n=3日" in text, text
    assert "99" not in text and "98" not in text, "兩週前的分數不得算進本週"


# ── 第 1 批首推日的一次性校正 ─────────────────────────────────────────────────

def test_migrates_overwritten_first_push_date(notify):
    """被重推洗成 09-10 的首推日要校正回 09-08，並留痕。"""
    s = notify._migrate_hedge_batch_1_date(
        {"hedge_batch_1": True, "hedge_batch_1_date": "2026-09-10"})
    assert s["hedge_batch_1_date"] == "2026-09-08"
    assert s["hedge_batch_1_date_migrated"] is True


def test_migration_is_idempotent_and_narrow(notify):
    """只咬那一筆：已校正過、日期不同、或旗標未立，一律原樣不動。"""
    already = notify._migrate_hedge_batch_1_date(
        {"hedge_batch_1": True, "hedge_batch_1_date": "2026-09-08"})
    assert already["hedge_batch_1_date"] == "2026-09-08"
    assert "hedge_batch_1_date_migrated" not in already

    other = notify._migrate_hedge_batch_1_date(
        {"hedge_batch_1": True, "hedge_batch_1_date": "2026-09-12"})
    assert other["hedge_batch_1_date"] == "2026-09-12"

    noflag = notify._migrate_hedge_batch_1_date({"hedge_batch_1_date": "2026-09-10"})
    assert noflag["hedge_batch_1_date"] == "2026-09-10"


def test_migration_applies_on_load(notify, state_file):
    """走正常讀檔路徑也要套用，不是只有直接呼叫才生效。"""
    _write(state_file, {"hedge_batch_1": True, "hedge_batch_1_date": "2026-09-10"})
    assert notify._load_escape_state()["hedge_batch_1_date"] == "2026-09-08"


def test_migration_does_not_mutate_caller_dict(notify):
    """比照 _migrate_legacy_closed_days：回新 dict，不就地改呼叫端的。"""
    orig = {"hedge_batch_1": True, "hedge_batch_1_date": "2026-09-10"}
    notify._migrate_hedge_batch_1_date(orig)
    assert orig["hedge_batch_1_date"] == "2026-09-10"
