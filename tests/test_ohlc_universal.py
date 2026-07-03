"""service/ohlc_universal.live_quote_freshness / _is_tw_trading_hours 單元測試。

背景：TWSE 免費源（Yahoo/Google 皆同）法定延遲約 20 分鐘，若沿用美股「age<15分＝盤中」
門檻，台股盤中會被誤判成「已收盤」（見 2026-07-03 handoff：盤中查詢顯示「已收盤（0h前）」）。
is_tw=True 時改用交易時段判斷，這裡驗證兩個市場的分流互不影響。"""
import datetime
import time

import pytest

from service.ohlc_universal import live_quote_freshness, _is_tw_trading_hours


def _q(age_sec, prev_close=200.0, price=206.5):
    return {"price": price, "ts": time.time() - age_sec, "prev_close": prev_close}


def test_tw_during_trading_hours_shows_delay_not_closed(monkeypatch):
    """台股盤中：即使 age 遠超過 15 分鐘（TWSE 免費源本就延遲 ~20 分），仍應標「盤中」而非「已收盤」。"""
    monkeypatch.setattr("service.ohlc_universal._is_tw_trading_hours", lambda: True)
    r = live_quote_freshness(_q(20 * 60), is_tw=True)
    assert "盤中" in r["label"]
    assert "已收盤" not in r["label"]
    assert "20分" in r["label"]


def test_tw_outside_trading_hours_shows_closed(monkeypatch):
    """台股非交易時段：沿用 age 分級顯示已收盤（Nh 前）。"""
    monkeypatch.setattr("service.ohlc_universal._is_tw_trading_hours", lambda: False)
    r = live_quote_freshness(_q(2 * 3600), is_tw=True)
    assert r["label"] == "⚪ 已收盤（2h 前）"


def test_tw_outside_trading_hours_long_stale_no_hour_suffix(monkeypatch):
    monkeypatch.setattr("service.ohlc_universal._is_tw_trading_hours", lambda: False)
    r = live_quote_freshness(_q(7 * 3600), is_tw=True)
    assert r["label"] == "⚪ 已收盤"


def test_us_logic_unaffected_by_tw_trading_hours(monkeypatch):
    """is_tw=False（預設）：即使此刻恰為台股交易時段，US/其他市場仍走原本 age<900s 門檻，不受影響。"""
    monkeypatch.setattr("service.ohlc_universal._is_tw_trading_hours", lambda: True)
    fresh = live_quote_freshness(_q(60))            # age=60s < 900s
    stale = live_quote_freshness(_q(20 * 60))        # age=20min，US 門檻下已算「收盤/非即時」
    assert fresh["label"] == "🟢 盤中即時"
    assert stale["label"] == "⚪ 已收盤（0h 前）"


def test_chg_pct_and_age_sec_present():
    r = live_quote_freshness(_q(60, prev_close=200.0, price=206.5), is_tw=False)
    assert r["chg_pct"] == pytest.approx(3.25)
    assert r["age_sec"] == pytest.approx(60, abs=1)


def test_missing_ts_treated_as_infinitely_stale():
    r = live_quote_freshness({"price": 100.0, "prev_close": 90.0}, is_tw=False)
    assert r["label"] == "⚪ 已收盤"
    assert r["age_sec"] == float("inf")


@pytest.mark.parametrize("hhmm,expected", [
    ((8, 59), False), ((9, 0), True), ((10, 16), True),
    ((13, 30), True), ((13, 31), False),
])
def test_is_tw_trading_hours_boundary(hhmm, expected):
    """2026-07-03 為週五，作固定基準日避開週末誤判。"""
    now = datetime.datetime(2026, 7, 3, *hhmm)
    assert _is_tw_trading_hours(now) is expected


def test_is_tw_trading_hours_weekend_false_even_in_window():
    """2026-07-04 為週六，即使時間落在 09:00-13:30 仍非交易時段。"""
    now = datetime.datetime(2026, 7, 4, 10, 0)
    assert _is_tw_trading_hours(now) is False
