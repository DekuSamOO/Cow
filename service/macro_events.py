"""
service/macro_events.py
總經事件行事曆（FOMC/CPI/PCE/非農）— 本地鏡像，與 Notion「BTC 總經事件」DB 同源

⚠️ 刻意獨立成零重依賴的小模組（只用 stdlib：os/json/logging/datetime）。原本這支函式
   跟 service/macro_data.py 的 FRED/yfinance 抓取函式放在一起，但那個檔案頂層寫死
   `import streamlit as st`（給其他函式的 `@st.cache_data` 裝飾器用）+ `import yfinance`。
   `get_next_macro_event()` 本身只讀本地 JSON，完全不需要這兩個重依賴，但**只要 import
   那個檔案就會連帶 import streamlit**（裝飾器在檔案載入時就要解析），而 `import streamlit`
   在公司網路環境下實測會卡住逾時（>10s 無回應，`import yfinance` 也要 7.6s）——watcher.py
   （BTC_WATCH.py._gather_externals，每小時刷新一次）沒有任何 timeout 保護，這步卡住會讓
   整個刷新（甚至整個 60s 迴圈）停住，非拋例外、try/except 攔不到。
   watcher.py／daily_line_notify.py 只需要「事件倒數天數」這一個資料，改吃這支輕量模組，
   徹底不必付這個 import 代價；service/macro_data.py 仍 re-export 供 dashboard 既有呼叫端
   （handler/tab_macro_compass.py）不必改 import 路徑。
"""
import os
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_MACRO_EVENTS_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "macro_events.json")


def get_next_macro_event(now=None) -> dict:
    """
    讀 db/macro_events.json，回傳最近的「未來（含當日）」重大總經事件：
      {days, date, type, importance}；無資料/無未來事件時 days=None。
    供 core/relative_high「總經逆風」維度的 event_within_days（事件臨近風險）。
    app 無 Notion 權限，故讀本地鏡像（每年底人工補下一年度）。
    """
    if now is None:
        now = datetime.now(timezone.utc).date()
    elif isinstance(now, datetime):
        now = now.date()
    try:
        with open(_MACRO_EVENTS_PATH, "r", encoding="utf-8") as f:
            events = json.load(f).get("events", [])
    except Exception as e:
        logger.warning(f"[macro_events] 讀取失敗：{e}")
        return {"days": None, "date": None, "type": None, "importance": None}

    future = []
    for ev in events:
        try:
            d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if d >= now:
            future.append((d, ev))
    if not future:
        return {"days": None, "date": None, "type": None, "importance": None}
    future.sort(key=lambda x: x[0])
    d, ev = future[0]
    return {"days": (d - now).days, "date": ev["date"],
            "type": ev.get("type"), "importance": ev.get("importance")}
