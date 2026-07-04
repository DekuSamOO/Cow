"""E3 觸發日誌＋e 鍵執行標記測試。不打網路。"""
import json
import sys
import types

import pandas as pd

sys.path.insert(0, ".")
import BTC_WATCH  # noqa: E402
import watcher  # noqa: E402
from core.watch_alerts import journal_append, journal_record, _mk  # noqa: E402


def test_journal_append_one_json_line_each(tmp_path):
    path = str(tmp_path / "logs" / "watch_journal.jsonl")   # logs/ 不存在 → 自動建
    journal_append({"a": 1}, path)
    journal_append({"b": "中文"}, path)
    lines = open(path, encoding="utf-8").read().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": "中文"}            # ensure_ascii=False 中文可讀


def test_journal_record_carries_snapshot_and_price():
    e = _mk("2330", "entry", "▶ 進入進場區", 955.0)
    rec = journal_record(e, {"trend": 38, "high": 53, "low": 20, "action_key": "RIDE"})
    assert rec["symbol"] == "2330" and rec["event"] == "entry"
    assert rec["price"] == 955.0 and rec["trend"] == 38 and rec["action_key"] == "RIDE"
    assert "T" in rec["ts"]                                 # 完整 ISO 時間戳（非 HH:MM）


def test_interruptible_wait_exec_key(monkeypatch):
    fake = types.SimpleNamespace(kbhit=lambda: True, getch=lambda: b"e")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    assert BTC_WATCH.interruptible_wait(5, nav=True) == "exec"


def test_universal_monitor_exec_logs_and_continues(monkeypatch, tmp_path):
    """e 鍵：記 executed 後迴圈繼續（不返回）；下一輪 q 才結束。"""
    m = watcher.UniversalMonitor({"kind": "us_stock", "display": "QQQ", "yahoo": "QQQ",
                                  "is_btc": False})
    df = pd.DataFrame({"close": range(60), "volume": [1] * 60})
    monkeypatch.setattr(m, "_fetch", lambda: df)
    monkeypatch.setattr(m, "render", lambda d: None)
    m._last_eff_price = 712.6
    cmds = iter(["exec", "quit"])
    monkeypatch.setattr(watcher, "interruptible_wait", lambda s, nav=False: next(cmds))
    logged = []
    monkeypatch.setattr(watcher, "journal_append", lambda rec, path=None: logged.append(rec))
    assert m.run() == "quit"
    assert len(logged) == 1
    assert logged[0]["event"] == "executed" and logged[0]["price"] == 712.6
    assert m._alert_banner and "已記錄執行標記" in m._alert_banner[0]
