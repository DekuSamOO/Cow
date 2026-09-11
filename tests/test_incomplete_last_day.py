# -*- coding: utf-8 -*-
"""
殘缺的最後一根日線不得當成收完的日線 —— 2026-09-11 立（實帳事故後）。

**事故經過**：collector 每天 09:00 本地跑一次，抓到當日 01:00 UTC 就 commit push，
所以 15m DB 裡**最後一天永遠是只有 5 根 K 棒的殘根**。平常無害——那個殘根就是
「今天」，`closed_daily_rsi()` 本來就排除當日。但 09-11 那天 collector 被 Ctrl+C
中斷（`LastTaskResult` 0xC000013A）沒推上去，雲端 checkout 到的 DB 最後一天變成
**09-10 的殘根**，隔天它就成了「昨天」而逃過當日排除：

    09-10 只有 00:00~01:00 的資料 → 收盤 78,180.69（真值 76,568.72）
    → 套保哨兵算出 RSI 58.97（真值 52.83）→ 第 2 批（<55）該響沒響，而且全程無聲。

無聲的原因：兩源對拍守門只在「主源認為這批到期」時才啟動，主源偏高就整批不算到期，
連「先不要建倉」警示都不會推。**主源自己錯的時候，那道守門擋不到。**

所以本檔測兩條紅線：
  A. 殘缺的最後一天要被丟掉（`drop_incomplete_last_day`）。
  B. 丟掉之後若收完日線落後超過上限，哨兵**必須出聲**，不得拿舊收盤照算。
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from service.local_db_reader import LAST_BAR_OF_DAY, drop_incomplete_last_day

_NOTIFY_PATH = os.path.join(_REPO, "scripts", "daily_line_notify.py")


def _fifteen_min(start, bars):
    """從 start 起連續 bars 根 15m K 棒的 index。"""
    return pd.date_range(start=start, periods=bars, freq="15min")


def _daily_from(idx, closes):
    return pd.DataFrame({"close": closes}, index=idx).resample("1D").agg(
        close=("close", "last")).dropna()


# ── A. 殘根要被丟掉 ─────────────────────────────────────────────────────────

def test_drops_last_day_when_only_first_hour_collected():
    """重現事故：最後一天只收到 00:00~01:00（5 根）。"""
    idx = list(_fifteen_min("2026-09-09 00:00", 96)) + list(_fifteen_min("2026-09-10 00:00", 5))
    daily = _daily_from(pd.DatetimeIndex(idx), list(range(len(idx))))
    assert str(daily.index[-1])[:10] == "2026-09-10", "前提：重採樣後確實多出一根殘根"

    out = drop_incomplete_last_day(daily, pd.Timestamp("2026-09-10 01:00"))
    assert str(out.index[-1])[:10] == "2026-09-09"
    assert len(out) == len(daily) - 1


def test_keeps_last_day_when_fully_collected():
    """收到 23:45 那根就算收完，不得誤丟。"""
    idx = _fifteen_min("2026-09-09 00:00", 96)
    daily = _daily_from(idx, list(range(96)))
    out = drop_incomplete_last_day(daily, pd.Timestamp("2026-09-09 23:45"))
    assert len(out) == len(daily)
    assert str(out.index[-1])[:10] == "2026-09-09"


def test_boundary_is_the_2345_bar():
    """23:30 還不算收完；23:45 才算。守門不可鬆一格。"""
    idx = _fifteen_min("2026-09-09 00:00", 95)
    daily = _daily_from(idx, list(range(95)))
    assert drop_incomplete_last_day(daily, pd.Timestamp("2026-09-09 23:30")).empty
    assert LAST_BAR_OF_DAY == pd.Timedelta(hours=23, minutes=45)


def test_interior_gaps_are_left_alone():
    """
    中間缺天是交易所停機之類的歷史事實，一起丟等於改寫回測輸入。
    只准動最後一根。
    """
    idx = list(_fifteen_min("2026-09-05 00:00", 96)) + list(_fifteen_min("2026-09-08 00:00", 96))
    daily = _daily_from(pd.DatetimeIndex(idx), list(range(len(idx))))
    out = drop_incomplete_last_day(daily, pd.Timestamp("2026-09-08 23:45"))
    assert len(out) == 2, "09-06、09-07 缺天不得被這道守門連坐"


def test_empty_frame_is_safe():
    assert drop_incomplete_last_day(pd.DataFrame(), pd.Timestamp("2026-09-10 01:00")).empty


# ── B. 收完日線過期時哨兵不得靜默 ────────────────────────────────────────────

@pytest.fixture(scope="module")
def notify():
    spec = importlib.util.spec_from_file_location("daily_line_notify", _NOTIFY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sent(notify, tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(tmp_path / "state.json"))
    box = []
    monkeypatch.setattr(notify, "send_line_message", lambda payload: box.append(payload))
    # 對拍源固定與主源同日同調：本檔測的是**主源自己的落後守門**，
    # 對拍日期若寫死就會變成「落後 N 天」而走進分歧分支，測試等於在測別的東西。
    monkeypatch.setattr(notify, "crosscheck_daily_rsi",
                        lambda *a, **k: (0.0, 100.0, _days_ago(1)))
    monkeypatch.setattr(notify, "_state_chain_broken", lambda *a, **k: False)
    return box


def _data(rsi_closed, closed_date):
    return {"rsi14": 30.0, "rsi14_closed": rsi_closed, "rsi_peak": 86.0,
            "rsi_closed_date": closed_date, "current_price": 76992.0}


def _days_ago(n):
    return str(datetime.now(timezone.utc).date() - timedelta(days=n))


def test_stale_closed_bar_alerts_and_blocks_entry(notify, sent):
    """
    收完日線落後 2 天＝資料源少了一天。此時 RSI 算的是前天的收盤，
    **不可照著推建倉，也不可靜默**。
    """
    notify.maybe_send_hedge_batch_alert(_data(63.9, _days_ago(2)))
    assert len(sent) == 1, "資料過期不得靜默"
    text = sent[0]["text"]
    # 用正式建倉訊息的專屬字串判別：告警本身也會帶「[套保建倉]」字樣，
    # 拿那個判會自己咬自己（同 test_hedge_batch_alert 的既有作法）。
    assert "全倉套保" not in text and "批觸發" not in text, "過期資料不得推成建倉指示"


def test_fresh_closed_bar_passes_through(notify, sent):
    """落後 1 天＝`closed_daily_rsi()` 能拿到的最新值，必須放行到正式建倉。"""
    notify.maybe_send_hedge_batch_alert(_data(63.9, _days_ago(1)))
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "第 1 批觸發" in text and "全倉套保" in text, "新鮮資料不得被守門誤擋"


def test_missing_closed_date_is_treated_as_untrustworthy(notify, sent):
    notify.maybe_send_hedge_batch_alert(_data(63.9, None))
    assert len(sent) == 1
    assert "全倉套保" not in sent[0]["text"] and "批觸發" not in sent[0]["text"]


def test_lag_helper_reads_iso_date(notify):
    assert notify._closed_bar_lag_days(_days_ago(1)) == 1
    assert notify._closed_bar_lag_days(_days_ago(5)) == 5
    assert notify._closed_bar_lag_days("not-a-date") is None
    assert notify._closed_bar_lag_days(None) is None


def test_incident_numbers_reproduce(notify):
    """
    把事故那天的兩個數字釘住，避免日後有人「順手」把守門放寬。
    上限只能是 1：`closed_daily_rsi()` 排除當日，最新就只可能是昨天。
    """
    assert notify.HEDGE_MAX_CLOSED_BAR_LAG_DAYS == 1
