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
