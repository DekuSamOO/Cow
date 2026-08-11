"""service/ohlc_universal.live_quote_freshness / _is_tw_trading_hours / is_daily_bar_forming /
resolve_live_volume 單元測試。

背景：TWSE 免費源（Yahoo/Google 皆同）法定延遲約 20 分鐘，若沿用美股「age<15分＝盤中」
門檻，台股盤中會被誤判成「已收盤」（見 2026-07-03 handoff：盤中查詢顯示「已收盤（0h前）」）。
is_tw=True 時改用交易時段判斷，這裡驗證兩個市場的分流互不影響。"""
import datetime
import time

import pytest

from service.ohlc_universal import (
    live_quote_freshness, _is_tw_trading_hours, _is_us_trading_hours,
    is_daily_bar_forming, resolve_live_volume, _tw_candidates, fetch_live_quote,
    classify_symbol, fetch_ohlc,
)


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


@pytest.mark.parametrize("hhmm,expected", [
    ((9, 29), False), ((9, 30), True), ((11, 0), True),
    ((16, 0), True), ((16, 1), False),
])
def test_is_us_trading_hours_boundary(hhmm, expected):
    """2026-07-03 為週五，作固定基準日避開週末誤判。"""
    now = datetime.datetime(2026, 7, 3, *hhmm)
    assert _is_us_trading_hours(now) is expected


def test_is_us_trading_hours_weekend_false_even_in_window():
    now = datetime.datetime(2026, 7, 4, 11, 0)
    assert _is_us_trading_hours(now) is False


# ---------------------------------------------------------------------------
# is_daily_bar_forming
# ---------------------------------------------------------------------------

def test_daily_bar_forming_tw_mid_session_last_bar_is_today():
    """台股盤中、最後一根日期＝今天 → 進行式（尚未結算）。"""
    now = datetime.datetime(2026, 7, 3, 10, 40)   # 週五 10:40，盤中
    assert is_daily_bar_forming(datetime.date(2026, 7, 3), True, now=now) is True


def test_daily_bar_forming_tw_mid_session_stale_cache_last_bar_is_yesterday():
    """台股盤中，但日線快取尚未刷到今天（最後一根仍是昨天已結算收盤）→ 非進行式，
    不可誤判為進行式而錯誤退回前兩天。"""
    now = datetime.datetime(2026, 7, 3, 9, 5)     # 剛開盤，快取可能還沒刷新
    assert is_daily_bar_forming(datetime.date(2026, 7, 2), True, now=now) is False


def test_daily_bar_forming_tw_after_close_last_bar_is_today_settled():
    """台股已收盤、最後一根日期＝今天（已結算）→ 非進行式，照常顯示今天收盤。"""
    now = datetime.datetime(2026, 7, 3, 14, 0)
    assert is_daily_bar_forming(datetime.date(2026, 7, 3), True, now=now) is False


def test_daily_bar_forming_us_mid_session():
    now = datetime.datetime(2026, 7, 2, 11, 0)    # 週四，美股盤中
    assert is_daily_bar_forming(datetime.date(2026, 7, 2), False, now=now) is True


def test_daily_bar_forming_us_weekend_false():
    now = datetime.datetime(2026, 7, 4, 11, 0)    # 週六
    assert is_daily_bar_forming(datetime.date(2026, 7, 4), False, now=now) is False


def test_daily_bar_forming_crypto_247_ignores_us_session():
    """幣對 24/7：美股已收盤（或週末）但最後一根＝UTC 今天 → 仍是進行式（量還在累積）。
    不特判會套美股時段，每天有 17.5 小時把未結算的今日棒當成已結算。"""
    now = datetime.datetime(2026, 7, 4, 22, 0)    # 週六深夜 UTC，美股沒開
    assert is_daily_bar_forming(datetime.date(2026, 7, 4), False, now=now, is_crypto=True) is True


def test_daily_bar_forming_crypto_yesterday_bar_settled():
    """幣對最後一根＝UTC 昨天（已跨日結算）→ 非進行式。"""
    now = datetime.datetime(2026, 7, 4, 0, 30)
    assert is_daily_bar_forming(datetime.date(2026, 7, 3), False, now=now, is_crypto=True) is False


# ---------------------------------------------------------------------------
# resolve_live_volume
# ---------------------------------------------------------------------------

def test_resolve_live_volume_fresh_value_used_directly():
    vol, note = resolve_live_volume(546342, None, 0, 60)
    assert (vol, note) == (546342, "")


def test_resolve_live_volume_missing_falls_back_to_cache_no_note_when_recent():
    """單次缺漏、快取剛更新不久（<2 個刷新週期）→ 沿用快取，不加標註（避免單次 blip 打擾使用者）。"""
    now = time.time()
    vol, note = resolve_live_volume(None, 546342, now - 30, refresh_sec=60, now=now)
    assert vol == 546342 and note == ""


def test_resolve_live_volume_missing_long_stale_adds_note():
    """缺漏且快取已超過 2 個刷新週期（>120s）→ 沿用快取但附註快取時間。"""
    now = time.time()
    vol, note = resolve_live_volume(None, 546342, now - 300, refresh_sec=60, now=now)
    assert vol == 546342
    assert "快取" in note and "300s" in note


def test_resolve_live_volume_missing_and_no_cache_returns_none():
    vol, note = resolve_live_volume(None, None, 0, 60)
    assert (vol, note) == (None, "")


def test_resolve_live_volume_fresh_zero_treated_as_missing():
    """live_volume=0（罕見但理論可能）視同缺漏 → 走快取路徑，而非顯示 0 股。"""
    now = time.time()
    vol, note = resolve_live_volume(0, 546342, now - 30, refresh_sec=60, now=now)
    assert vol == 546342


# ---------------------------------------------------------------------------
# _tw_candidates / fetch_live_quote 上櫃 .TWO 備援
# ---------------------------------------------------------------------------

def test_tw_candidates_appends_two_suffix_for_tw():
    assert _tw_candidates("6509.TW") == ["6509.TW", "6509.TWO"]


def test_tw_candidates_unchanged_for_non_tw_symbol():
    assert _tw_candidates("AAPL") == ["AAPL"]
    assert _tw_candidates("BTC-USD") == ["BTC-USD"]


class _FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _meta_payload(price=55.3, ts=1700000000, prev_close=52.3, volume=5_000_000):
    return {"chart": {"result": [{"meta": {
        "regularMarketPrice": price, "regularMarketTime": ts,
        "previousClose": prev_close, "regularMarketVolume": volume,
    }}]}}


def test_fetch_live_quote_tpex_falls_back_to_two_when_tw_404(monkeypatch):
    """上市 .TW 404（如 6509 實為上櫃股）→ 應自動改試 .TWO 並成功，而非整體回傳空 dict
    （2026-07-03 使用者回報：切到上櫃股 6509 後現價/即時成交量消失，查證是 .TW 端點 404，
    非網路波動；fetch_ohlc 早有此備援，fetch_live_quote 原本沒有）。"""
    calls = []

    def fake_get(self, url, **kw):
        calls.append(url)
        if url.endswith("/6509.TW"):
            return _FakeResp(status=404)
        if url.endswith("/6509.TWO"):
            return _FakeResp(_meta_payload(price=55.3, volume=5_814_250))
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("requests.Session.get", fake_get)
    q = fetch_live_quote("6509.TW")
    assert q == {"price": 55.3, "ts": 1700000000, "prev_close": 52.3, "volume": 5_814_250}
    assert len(calls) == 2   # 先試 .TW 失敗、才試 .TWO


def test_fetch_live_quote_twse_succeeds_on_first_candidate(monkeypatch):
    """上市股 .TW 第一candidate 就成功，不應多打 .TWO（避免上市股白白多一次網路請求）。"""
    calls = []

    def fake_get(self, url, **kw):
        calls.append(url)
        return _FakeResp(_meta_payload(price=211.5))

    monkeypatch.setattr("requests.Session.get", fake_get)
    q = fetch_live_quote("6782.TW")
    assert q["price"] == 211.5
    assert len(calls) == 1


def test_fetch_live_quote_both_candidates_fail_returns_empty_dict(monkeypatch):
    def fake_get(self, url, **kw):
        return _FakeResp(status=404)

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert fetch_live_quote("99999999.TW") == {}


def test_fetch_live_quote_non_tw_symbol_no_fallback_attempted(monkeypatch):
    """非台股代號（無 .TW 後綴）：_tw_candidates 只有一個候選，失敗直接回空 dict。"""
    calls = []

    def fake_get(self, url, **kw):
        calls.append(url)
        return _FakeResp(status=404)

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert fetch_live_quote("AAPL") == {}
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# classify_symbol（W-9：邊角案例參數化測試，2026-07-06 補）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,kind,display,yahoo,is_btc", [
    ("2330",      "tw_stock", "2330",    "2330.TW",  False),   # 台股上市 4 碼
    ("00878",     "tw_stock", "00878",   "00878.TW", False),   # 台股 ETF 5 碼
    ("6509",      "tw_stock", "6509",    "6509.TW",  False),   # 台股上櫃（classify 無法預知，統一先給 .TW，實際解析靠 _tw_candidates）
    ("2330.TW",   "tw_stock", "2330",    "2330.TW",  False),   # 已帶 .TW 後綴
    ("QQQ",       "us_stock", "QQQ",     "QQQ",      False),   # 美股 ETF
    ("BRK.B",     "us_stock", "BRK.B",   "BRK-B",    False),   # W-10：class share，Yahoo 需 - 不是 .
    ("BF.B",      "us_stock", "BF.B",    "BF-B",     False),
    ("btcusdt",   "crypto",   "BTCUSDT", "BTC-USD",  True),    # 小寫輸入須正規化
    ("BTC",       "crypto",   "BTCUSDT", "BTC-USD",  True),
    ("BTC-USD",   "crypto",   "BTCUSDT", "BTC-USD",  True),
    ("XBTUSD",    "crypto",   "BTCUSDT", "BTC-USD",  True),
    ("ETHUSDT",   "crypto",   "ETHUSDT", "ETH-USD",  False),   # 非 BTC 幣對
    ("SOL-USD",   "crypto",   "SOLUSDT", "SOL-USD",  False),
    ("ETHUSD",    "crypto",   "ETHUSDT", "ETH-USD",  False),
])
def test_classify_symbol_matrix(raw, kind, display, yahoo, is_btc):
    info = classify_symbol(raw)
    assert info["kind"] == kind
    assert info["display"] == display
    assert info["yahoo"] == yahoo
    assert info["is_btc"] is is_btc


def test_classify_symbol_empty_raises():
    with pytest.raises(ValueError):
        classify_symbol("")
    with pytest.raises(ValueError):
        classify_symbol("   ")


def test_classify_symbol_crypto_has_binance_fields():
    info = classify_symbol("ETHUSDT")
    assert info["binance"] == "ETHUSDT"
    assert info["coin"] == "ETHUSD_PERP"
    assert info["base"] == "ETH"


# ---------------------------------------------------------------------------
# fetch_ohlc：歷史長度預設 / 幽靈零量列（2026-08-11）
# ---------------------------------------------------------------------------

def _chart_payload(volumes, ts0=1700000000):
    n = len(volumes)
    return {"chart": {"result": [{
        "timestamp": [ts0 + i * 86400 for i in range(n)],
        "indicators": {"quote": [{
            "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
            "close": [100.0] * n, "volume": list(volumes),
        }]},
    }]}}


def test_fetch_ohlc_default_range_is_10y(monkeypatch):
    """量能分位母體＝本函式抓回的歷史，須與校準面板（expanding，自 2016-01-01）對齊。
    預設縮回 2y 會系統性推高分位（6782 實測 93.5 vs 83.5 分位、量能維 12/18 vs 6/18）。"""
    seen = {}

    def fake_get(self, url, **kw):
        seen["range"] = kw["params"]["range"]
        seen["interval"] = kw["params"]["interval"]
        return _FakeResp(_chart_payload([1_000_000] * 5))

    monkeypatch.setattr("requests.Session.get", fake_get)
    fetch_ohlc("2330.TW")
    assert seen["range"] == "10y"      # 不可改回 2y；也不可改 max（Yahoo 會降頻成週/月線）
    assert seen["interval"] == "1d"


def test_fetch_ohlc_phantom_zero_volume_becomes_nan(monkeypatch):
    """Yahoo 對台股偶爾回「有價無量」幽靈列（實測近 10 年 6782 1 筆 / 2454 4 筆 / 6509 9 筆）。
    量欄轉 NaN（不進量能母體、mean 自動略過），但**價格那根保留**——它是真的，
    MA/RSI/ATR 不該因此少一天。"""
    def fake_get(self, url, **kw):
        return _FakeResp(_chart_payload([1_000_000, 0, 2_000_000]))

    monkeypatch.setattr("requests.Session.get", fake_get)
    df = fetch_ohlc("6782.TW")
    assert len(df) == 3                                  # 列沒被刪掉
    assert df["close"].notna().all()
    assert df["volume"].isna().sum() == 1
    assert df["volume"].dropna().tolist() == [1_000_000, 2_000_000]
