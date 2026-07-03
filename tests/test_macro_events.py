"""service/macro_events.get_next_macro_event 單元測試。

背景：這支函式原本跟 service/macro_data.py 的 FRED/yfinance 抓取函式放同一檔，但那個
檔案頂層 `import streamlit as st`（給其他函式的 `@st.cache_data` 裝飾器用），2026-07-03
實測公司網路環境下 `import streamlit` 本身會卡住逾時（>10s 無回應）。get_next_macro_event
只讀本地 JSON，完全不需要 streamlit/yfinance，抽成獨立零重依賴模組解決此問題（watcher.py
的 BTC_WATCH._gather_externals 因此從卡住 >10s 變成 0.02s，見 core/../BTC_WATCH.py 呼叫端）。
"""
import json
import subprocess
import sys
import os
from datetime import datetime

import pytest

from service.macro_events import get_next_macro_event

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_events(path, events):
    path.write_text(json.dumps({"events": events}), encoding="utf-8")


def test_returns_nearest_future_event(tmp_path, monkeypatch):
    p = tmp_path / "macro_events.json"
    _write_events(p, [
        {"date": "2026-08-01", "type": "FOMC", "importance": "高"},
        {"date": "2026-07-14", "type": "CPI", "importance": "高"},
        {"date": "2026-06-01", "type": "NFP", "importance": "中"},   # 過去事件，應忽略
    ])
    monkeypatch.setattr("service.macro_events._MACRO_EVENTS_PATH", str(p))
    r = get_next_macro_event(now=datetime(2026, 7, 3))
    assert r == {"days": 11, "date": "2026-07-14", "type": "CPI", "importance": "高"}


def test_event_today_counts_as_days_zero(tmp_path, monkeypatch):
    """含當日：事件日期＝now 當天，days 應為 0（不是被當成過去事件排除）。"""
    p = tmp_path / "macro_events.json"
    _write_events(p, [{"date": "2026-07-03", "type": "CPI", "importance": "高"}])
    monkeypatch.setattr("service.macro_events._MACRO_EVENTS_PATH", str(p))
    r = get_next_macro_event(now=datetime(2026, 7, 3))
    assert r["days"] == 0


def test_no_future_events_returns_none_days(tmp_path, monkeypatch):
    p = tmp_path / "macro_events.json"
    _write_events(p, [{"date": "2026-01-01", "type": "FOMC", "importance": "高"}])
    monkeypatch.setattr("service.macro_events._MACRO_EVENTS_PATH", str(p))
    r = get_next_macro_event(now=datetime(2026, 7, 3))
    assert r == {"days": None, "date": None, "type": None, "importance": None}


def test_missing_file_returns_none_days_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("service.macro_events._MACRO_EVENTS_PATH",
                         str(tmp_path / "does_not_exist.json"))
    r = get_next_macro_event(now=datetime(2026, 7, 3))
    assert r == {"days": None, "date": None, "type": None, "importance": None}


def test_now_accepts_datetime_or_date():
    """now 傳 datetime 或 date 皆可（datetime 會被轉成 .date()）。"""
    r1 = get_next_macro_event(now=datetime(2026, 1, 1))
    r2 = get_next_macro_event(now=datetime(2026, 1, 1).date())
    assert r1 == r2


def test_import_has_no_heavy_deps():
    """迴歸鎖：獨立子行程驗證 import service.macro_events 不會連帶拉入 streamlit/yfinance
    （用子行程取得乾淨 sys.modules，避免同一 pytest session 內其他測試已 import 污染判斷）。"""
    code = (
        "import sys\n"
        "import service.macro_events\n"
        "assert 'streamlit' not in sys.modules, 'streamlit 不該被連帶 import'\n"
        "assert 'yfinance' not in sys.modules, 'yfinance 不該被連帶 import'\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "OK" in r.stdout
