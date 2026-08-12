"""watcher.py 批次1 穩定性測試（W-1 永久失敗回上層 / W-2 刷新退舊快取 / W-5 prompt 捕例外）。

對應 plan：Obsidian\\Github\\Cow\\20260704plan_watcher稽核.md
不打網路：fetch/等待全 monkeypatch。
"""
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, ".")
import watcher  # noqa: E402
from watcher import UniversalMonitor  # noqa: E402

_INFO_US = {"kind": "us_stock", "display": "QQQ", "yahoo": "QQQ", "is_btc": False}
_INFO_TW = {"kind": "tw_stock", "display": "2330", "yahoo": "2330.TW", "is_btc": False}


def _mk_monitor():
    return UniversalMonitor(dict(_INFO_US))


def _df(n=60):
    return pd.DataFrame({"close": range(n), "volume": [100] * n})


# ── W-1：永久性失敗（無資料）直接回代號選單 ──────────────────────────────────

def test_permanent_runtime_error_returns_back_immediately(monkeypatch):
    m = _mk_monitor()
    monkeypatch.setattr(m, "_fetch", lambda: (_ for _ in ()).throw(RuntimeError("無資料：QQQ")))
    waits = []
    monkeypatch.setattr(watcher, "interruptible_wait", lambda s, nav=False: waits.append(s))
    assert m.run() == "back"
    assert waits == []          # 永久失敗不進任何重試等待


# ── W-1：暫時性失敗連續 3 次自動回上層，等待走 interruptible_wait（可按鍵）──

def test_transient_failures_capped_then_back(monkeypatch):
    m = _mk_monitor()
    monkeypatch.setattr(m, "_fetch", lambda: (_ for _ in ()).throw(ValueError("timeout")))
    waits = []

    def fake_wait(seconds, nav=False):
        waits.append((seconds, nav))
        return None

    monkeypatch.setattr(watcher, "interruptible_wait", fake_wait)
    assert m.run() == "back"
    # 3 次上限：前 2 次失敗各等一次（第 3 次直接回上層），且等待帶 nav=True（b/q 可用）
    assert waits == [(10, True), (10, True)]


def test_transient_wait_honors_quit_key(monkeypatch):
    m = _mk_monitor()
    monkeypatch.setattr(m, "_fetch", lambda: None)   # 資料不足路徑
    monkeypatch.setattr(watcher, "interruptible_wait", lambda s, nav=False: "quit")
    assert m.run() == "quit"    # 重試等待期間按 q 立即結束


def test_insufficient_data_counts_as_transient(monkeypatch):
    m = _mk_monitor()
    monkeypatch.setattr(m, "_fetch", lambda: _df(10))   # <50 根
    monkeypatch.setattr(watcher, "interruptible_wait", lambda s, nav=False: None)
    assert m.run() == "back"


# ── W-2：每小時刷新失敗且有快取 → 沿用舊快取＋標註＋5 分後再試 ──────────────

def test_refresh_failure_falls_back_to_cache(monkeypatch):
    m = _mk_monitor()
    cache = _df()
    m._daily_cache = cache
    m._daily_ts = time.time() - m.DAILY_REFRESH_SEC - 100   # 已過期 → 會嘗試刷新
    monkeypatch.setattr(watcher, "fetch_ohlc",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("Yahoo 503")))
    out = m._fetch()
    assert out is cache                                     # 退回舊快取，畫面不全滅
    assert m._stale_note and "沿用" in m._stale_note
    # 下次重試排在 RETRY_REFRESH_SEC（±10s 容差）後，而非每 60s 硬撞故障源
    next_retry = m.DAILY_REFRESH_SEC - (time.time() - m._daily_ts)
    assert abs(next_retry - m.RETRY_REFRESH_SEC) < 10


def test_refresh_failure_without_cache_reraises(monkeypatch):
    m = _mk_monitor()
    monkeypatch.setattr(watcher, "fetch_ohlc",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("Yahoo 503")))
    with pytest.raises(ConnectionError):
        m._fetch()              # 首抓失敗仍拋給 run() 走重試/回上層


def test_successful_refresh_clears_stale_note(monkeypatch):
    m = _mk_monitor()
    m._stale_note = "舊標註"
    m._daily_cache, m._daily_ts = _df(), 0.0                # 過期 → 觸發刷新
    monkeypatch.setattr(watcher, "fetch_ohlc", lambda *a, **k: _df())
    monkeypatch.setattr(watcher, "calculate_technical_indicators", lambda df: df)
    m._fetch()
    assert m._stale_note is None


# ── W-5：提示符 Ctrl+C / EOF → 乾淨結束 ─────────────────────────────────────

@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_prompt_interrupt_exits_cleanly(monkeypatch, exc):
    monkeypatch.setattr(sys, "argv", ["watcher.py"])
    monkeypatch.setattr(watcher, "_prompt_symbol",
                        lambda: (_ for _ in ()).throw(exc()))
    assert watcher.main() is None       # 不吐 traceback


# ── 籌碼源故障不得中斷監控（2026-08-11，模擬 TWSE 逾時實測出來的洞）───────────

def _tw_monitor(monkeypatch):
    """台股 monitor，日線來源 mock 成功；籌碼由各測試自行注入。"""
    m = UniversalMonitor(dict(_INFO_TW))
    n = 60
    df = pd.DataFrame({"close": range(n), "volume": [100] * n},
                      index=pd.date_range("2026-01-01", periods=n, freq="D"))
    monkeypatch.setattr(watcher, "fetch_ohlc", lambda *a, **k: df)
    monkeypatch.setattr(watcher, "calculate_technical_indicators", lambda d: d)
    return m, df


def test_chip_failure_does_not_break_fetch(monkeypatch):
    """TWSE/TPEx 掛掉時，價格/趨勢/量能都還是好的 → _fetch 不得把例外丟給 run()
    （否則整頁換成「擷取失敗」＋等 10 秒，明明只有籌碼那一塊拿不到）。"""
    m, df = _tw_monitor(monkeypatch)
    import service.tw_chip as tw_chip
    monkeypatch.setattr(tw_chip, "get_chip_bundle",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TWSE 逾時")))
    out = m._fetch()                       # 不拋
    assert out is df
    assert m._chip is None                 # 首抓就失敗 → 無籌碼
    assert m._chip_err and "TWSE" in m._chip_err


def test_chip_failure_keeps_previous_bundle(monkeypatch):
    """已有上一輪籌碼時刷新失敗 → 保留舊的（TWSE 是 EOD 檔，舊一天仍是真資料，
    畫面的 as_of 會如實顯示是哪天），不要清成 None 讓面板整塊消失。"""
    m, _ = _tw_monitor(monkeypatch)
    import service.tw_chip as tw_chip
    stale = {"as_of": "20260810", "valuation": {"pe": 13.0, "pb": 3.0}}
    monkeypatch.setattr(tw_chip, "get_chip_bundle", lambda *a, **k: stale)
    monkeypatch.setattr(tw_chip, "get_shares_outstanding", lambda *a, **k: 1000)
    m._fetch()
    assert m._chip is stale and m._chip_err is None
    m._daily_ts = 0.0                      # 逼出下一輪刷新
    monkeypatch.setattr(tw_chip, "get_chip_bundle",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("斷線")))
    m._fetch()
    assert m._chip is stale                # 續用舊 EOD 檔
    assert m._chip_err and "斷線" in m._chip_err


def test_chip_success_clears_error_flag(monkeypatch):
    m, _ = _tw_monitor(monkeypatch)
    m._chip_err = "上一輪的錯"
    import service.tw_chip as tw_chip
    monkeypatch.setattr(tw_chip, "get_chip_bundle", lambda *a, **k: {"as_of": "20260811"})
    monkeypatch.setattr(tw_chip, "get_shares_outstanding", lambda *a, **k: 1000)
    m._fetch()
    assert m._chip_err is None


# ── 背景標的巡檢節流（Yahoo 429 前例：原本 9 req/min ≈ 540 req/hr）─────────────

def _bg_calls(m, monkeypatch, plans, now):
    """跑**正式**的 _background_events，回傳本次實際打出去的 yahoo symbol 清單。"""
    calls = []
    monkeypatch.setattr(watcher, "fetch_live_quote", lambda s: calls.append(s) or {})
    monkeypatch.setattr(watcher.time, "time", lambda: now)
    m._background_events(m.display.upper(), plans)
    return calls


def test_background_quotes_capped_and_exclude_self(monkeypatch):
    """先剔本標的再取上限 → 實際發數恆為 BG_QUOTE_MAX。
    原寫法是先 slice[:9] 後 skip，本標的不在計畫內時會多打一發，與註解「上限 8 檔」對不上。"""
    m = UniversalMonitor(dict(_INFO_US))
    plans = {f"T{i}": {} for i in range(12)}            # 12 檔、皆非本標的（QQQ）
    assert len(_bg_calls(m, monkeypatch, plans, now=1000.0)) == m.BG_QUOTE_MAX == 8
    m2 = UniversalMonitor(dict(_INFO_US))
    with_self = {"QQQ": {}, **{f"T{i}": {} for i in range(12)}}
    calls2 = _bg_calls(m2, monkeypatch, with_self, now=1000.0)
    assert len(calls2) == 8 and "QQQ" not in calls2    # 本標的不重複打（現價線已每 60s 抓）


def test_background_quotes_throttled_between_rounds(monkeypatch):
    """60s 主迴圈不再每輪全掃（原 9 req/min ≈ 540 req/hr，此 repo 有 Yahoo 429 前例）：
    BG_QUOTE_SEC 內重入應零請求，屆期才再打。"""
    m = UniversalMonitor(dict(_INFO_US))
    plans = {f"T{i}": {} for i in range(3)}
    assert len(_bg_calls(m, monkeypatch, plans, now=1000.0)) == 3        # 首輪照打
    assert _bg_calls(m, monkeypatch, plans, now=1000.0 + 60) == []       # 節流窗內
    assert _bg_calls(m, monkeypatch, plans, now=1000.0 + 299) == []
    assert len(_bg_calls(m, monkeypatch, plans, now=1000.0 + 300)) == 3  # 屆期再打


def test_background_events_tagged_and_state_kept(monkeypatch):
    """觸價事件要標〔背景標的〕，且 alert state 逐檔保留（去重/遲滯才不會每輪重放）。"""
    m = UniversalMonitor(dict(_INFO_US))
    monkeypatch.setattr(watcher, "fetch_live_quote", lambda s: {"price": 100.0})
    monkeypatch.setattr(watcher, "check_price_events",
                        lambda p, price, st: ([{"symbol": "T0", "msg": "到價"}], {"armed": True}))
    evs = m._background_events("QQQ", {"T0": {}})
    assert evs and evs[0]["msg"].endswith("〔背景標的〕")
    assert m._alert_state["T0"] == {"armed": True}
