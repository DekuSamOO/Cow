# -*- coding: utf-8 -*-
"""
主源那根收完日線是殘值時，`sop_status` 不得給門檻判定 —— 2026-09-16 立（實帳事故後）。

**事故經過**：使用者 09-16 08:57 依派網 app 自己建了套保第 3 批；而同一天 08:19／08:53
兩次 `sop_status` 查詢都回「未達門檻」（主源 RSI 53.75）。事後兩源實算：09-15 收完日線
RSI 48.26（Cow 15m DB 重採樣）／48.15（Binance fapi 日線）**皆 <50，第 3 批確實觸發**。

差在主源：`db/cache/BTC_HISTORY.csv` 的 09-15 那列是**盤中殘值**（收 77,216、量 397，
截在台北 15:00）。`REFETCH_TAIL_DAYS=3` 的重抓沒能覆蓋它——collector 台北 09:00 才更新
15m DB，在那之前來源鏈第一順位 `read_btc_daily()` 會丟掉不完整的最後一根。

**為什麼既有守門擋不到**：
  - `HEDGE_MAX_CLOSED_BAR_LAG_DAYS` 看的是「日期落後幾天」，那列日期是 09-15、lag=1 完全正常。
  - 兩源對拍只在「主源認為這批到期」時才啟動；主源偏高 → 整批不算到期 → 連警示都不會推。
壞的是**內容**不是日期，所以本檔測的是「拿 15m DB 同日收盤對帳」這道新守門。

紅線兩條：
  A. 主源收盤與 15m DB 同日收盤差超過門檻 → 必須標不可信，且三批判定不得再印「未達門檻」。
  B. 正常資料（兩源同一天收盤一致）不得被誤擋——989 天實測差異中位數 0.000%。
"""
import importlib.util
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_SOP_PATH = os.path.join(_REPO, "scripts", "sop_status.py")
_DAY = "2026-09-15"


@pytest.fixture(scope="module")
def sop():
    spec = importlib.util.spec_from_file_location("sop_status", _SOP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _main_df(close):
    """主源日線：只需要 index 與 close 兩樣，`_main_source_stale` 只讀這兩個。"""
    return pd.DataFrame({"close": [76000.0, close]},
                        index=pd.to_datetime(["2026-09-14", _DAY]))


def _fake_15m(monkeypatch, sop, close, bars=96):
    """把 15m DB 換成假的：回傳 (open_time_ms, close) 列表，由守門自己重採樣。"""
    base = pd.Timestamp(_DAY, tz="UTC")
    rows = [(int((base + pd.Timedelta(minutes=15 * i)).timestamp() * 1000),
             close if i == bars - 1 else close - 50) for i in range(bars)]

    class _Con:
        def execute(self, *a, **k):
            return rows

        def close(self):
            pass

    monkeypatch.setattr(sop, "_span_days", sop._span_days)      # 佔位，確保 monkeypatch 生效
    import sqlite3
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _Con())
    import glob
    monkeypatch.setattr(glob, "glob", lambda *a, **k: [os.path.join(_REPO, "db", "btcusdt_15m_2026.db")])


def test_partial_bar_in_main_source_is_flagged(sop, monkeypatch):
    """重現事故：主源 77,216，15m DB 真值 75,644.48（差 2.08%）→ 必須標不可信。"""
    _fake_15m(monkeypatch, sop, 75644.48)
    msg = sop._main_source_stale(_main_df(77216.0), _DAY)
    assert msg, "主源殘值必須被標出來，不得靜默"
    assert "疑似盤中殘值" in msg and "2.0" in msg


def test_matching_close_passes(sop, monkeypatch):
    """正常日：兩源同日收盤一致 → 不得誤擋（989 天實測差異中位數 0.000%）。"""
    _fake_15m(monkeypatch, sop, 75644.48)
    assert sop._main_source_stale(_main_df(75644.48), _DAY) is None


def test_small_gap_within_threshold_passes(sop, monkeypatch):
    """門檻內的微小差異（0.1%）放行——兩源本來就可能有零星尾差。"""
    _fake_15m(monkeypatch, sop, 75644.48)
    assert sop._main_source_stale(_main_df(75644.48 * 1.001), _DAY) is None


def test_incomplete_day_in_15m_db_is_flagged(sop, monkeypatch):
    """15m DB 那天只收到一部分（collector 還沒跑完）→ 也要標，不可拿來對帳後放行。"""
    _fake_15m(monkeypatch, sop, 75644.48, bars=5)
    msg = sop._main_source_stale(_main_df(75644.48), _DAY)
    assert msg and "尚未收齊" in msg


def test_threshold_is_pinned(sop):
    """門檻釘住：0.3% 來自 989 天實測（中位/90/99 分位皆 0.000%，事故日 2.077%）。
    有人想放寬時，這條會先擋下來並逼他回去看那份分布。"""
    assert sop.MAIN_SOURCE_MAX_CLOSE_GAP_PCT == 0.3
