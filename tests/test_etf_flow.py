"""service/etf_flow._summarize 單元測試 — 重點：佔位 0.0 不打斷連續流出/流入 streak。"""
from service.etf_flow import _summarize


def test_placeholder_zero_latest_does_not_break_outflow_streak():
    # 真實情境重現：一串流出，最新日是 Farside 佔位 0.0（06-30）
    data = {
        "2026-06-24": -469.0, "2026-06-25": -691.7, "2026-06-26": -444.5,
        "2026-06-29": -231.0, "2026-06-30": 0.0,
    }
    s = _summarize(data)
    # 0.0 被過濾 → latest 應為 06-29 的真實流出、streak 涵蓋全部 4 個流出日
    assert s["latest_date"] == "2026-06-29"
    assert s["latest"] == -231.0
    assert s["consecutive_outflow_days"] == 4
    assert s["consecutive_inflow_days"] == 0
    assert s["n"] == 4                      # 0.0 不計入


def test_zero_in_middle_spans_streak():
    # 0.0 夾在流出串中間 → 過濾後 streak 應跨越
    data = {"2026-06-01": -100.0, "2026-06-02": 0.0, "2026-06-03": -50.0}
    s = _summarize(data)
    assert s["consecutive_outflow_days"] == 2
    assert s["latest"] == -50.0


def test_inflow_streak_and_cum5d():
    data = {f"2026-06-1{i}": v for i, v in enumerate([10.0, 20.0, 30.0, 40.0, 50.0])}
    s = _summarize(data)
    assert s["consecutive_inflow_days"] == 5
    assert s["consecutive_outflow_days"] == 0
    assert s["cum_5d"] == 150.0


def test_all_zero_or_empty_is_no_data():
    assert _summarize({})["n"] == 0
    z = _summarize({"2026-06-30": 0.0, "2026-06-29": 0.0})
    assert z["n"] == 0 and z["latest"] is None and z["consecutive_outflow_days"] == 0
