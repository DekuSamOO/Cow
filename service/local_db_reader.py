"""
service/local_db_reader.py
讀取本地端預先收集的 BTC/USDT 15m SQLite 資料庫

由 collector/btc_price_collector.py 在本地端執行後生成，
commit push 到 GitHub，Streamlit Cloud 直接讀取（免 API 呼叫）。

目錄結構：
  db/
    btcusdt_15m_2013.db
    btcusdt_15m_2014.db
    ...
    btcusdt_15m_2026.db
"""

import os
import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime, timezone
from typing import Optional

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
# 同時支援從 repo 根目錄或子目錄呼叫
_SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT   = os.path.dirname(_SERVICE_DIR)
DB_DIR       = os.path.join(_REPO_ROOT, "db")


# ══════════════════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════════════════

def get_available_years() -> list[int]:
    """掃描 db/ 目錄，回傳有 SQLite 資料的年份列表（排序）。"""
    if not os.path.exists(DB_DIR):
        return []
    years = []
    for fname in os.listdir(DB_DIR):
        if fname.startswith("btcusdt_15m_") and fname.endswith(".db"):
            try:
                year = int(fname.replace("btcusdt_15m_", "").replace(".db", ""))
                years.append(year)
            except ValueError:
                pass
    return sorted(years)


def _read_single_year(year: int, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> pd.DataFrame:
    """讀取單一年份 DB，回傳 DatetimeIndex 的 OHLCV DataFrame（已排序）。"""
    db_path = os.path.join(DB_DIR, f"btcusdt_15m_{year}.db")
    if not os.path.exists(db_path):
        return pd.DataFrame()

    conn = sqlite3.connect(db_path)
    try:
        conditions, params = [], []
        if start_ms is not None:
            conditions.append("open_time >= ?")
            params.append(start_ms)
        if end_ms is not None:
            conditions.append("open_time <= ?")
            params.append(end_ms)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT open_time, open, high, low, close, volume FROM klines{where} ORDER BY open_time"
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(None)
    df.set_index("date", inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


# ══════════════════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════════════════

def has_local_data() -> bool:
    """快速檢查：是否有任何本地 15m DB 檔案存在。"""
    return len(get_available_years()) > 0


def get_latest_local_price() -> Optional[float]:
    """從本地 15m DB 取出最新一筆收盤價（不帶 cache，供即時備援用）。"""
    for year in reversed(get_available_years()):
        db_path = os.path.join(DB_DIR, f"btcusdt_15m_{year}.db")
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT close FROM klines ORDER BY open_time DESC LIMIT 1"
                ).fetchone()
                if row:
                    return float(row[0])
            finally:
                conn.close()
        except Exception:
            continue
    return None


@st.cache_data(ttl=86400)
def read_btc_15m(start_date: str = "2017-01-01", end_date: Optional[str] = None) -> pd.DataFrame:
    """
    讀取本地 BTC/USDT 15m K 線，合併多個年度資料庫。

    參數：
      start_date : "YYYY-MM-DD" 格式，預設 2017-01-01
      end_date   : "YYYY-MM-DD" 格式，預設為今天

    回傳：DatetimeIndex（UTC 去時區）的 OHLCV DataFrame，15m 間隔
    """
    years = get_available_years()
    if not years:
        return pd.DataFrame()

    # UTC-aware timestamps prevent 8-hour offset bug when running in UTC+8 (Taiwan)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = (datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if end_date else datetime.now(timezone.utc))

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    # 只讀取需要的年份
    needed = [y for y in years if y >= start_dt.year and y <= end_dt.year]
    if not needed:
        return pd.DataFrame()

    dfs = []
    for year in needed:
        df = _read_single_year(year, start_ms, end_ms)
        if not df.empty:
            dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    result = pd.concat(dfs)
    result = result[~result.index.duplicated(keep="last")]
    result.sort_index(inplace=True)
    return result


@st.cache_data(ttl=86400)
def read_btc_daily(start_date: str = "2015-01-01") -> pd.DataFrame:
    """
    將本地 15m 數據重採樣為日線 OHLCV。
    用於替換 service/market_data.py 的遠端 API 呼叫，
    解決 Streamlit Cloud 抓不到 2015 年以來完整歷史的問題。

    回傳：DatetimeIndex 的日線 DataFrame（open/high/low/close/volume）
    """
    df_15m = read_btc_15m(start_date=start_date)
    if df_15m.empty:
        return pd.DataFrame()

    df_daily = df_15m.resample("1D").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["close"])

    return df_daily


def get_coverage_info() -> dict:
    """
    回傳本地 DB 的覆蓋範圍資訊，供 Streamlit 顯示。
    {
      "years": [2017, 2018, ...],
      "total_candles": 123456,
      "earliest": "2017-08-17",
      "latest": "2026-02-24",
    }
    """
    years = get_available_years()
    if not years:
        return {"years": [], "total_candles": 0, "earliest": None, "latest": None}

    total = 0
    earliest_ms = None
    latest_ms   = None

    for year in years:
        db_path = os.path.join(DB_DIR, f"btcusdt_15m_{year}.db")
        if not os.path.exists(db_path):
            continue
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(open_time), MAX(open_time) FROM klines"
            ).fetchone()
        if row and row[0]:
            total += row[0]
            if earliest_ms is None or row[1] < earliest_ms:
                earliest_ms = row[1]
            if latest_ms is None or row[2] > latest_ms:
                latest_ms = row[2]

    def _fmt(ms):
        if ms is None:
            return None
        from datetime import timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "years":         years,
        "total_candles": total,
        "earliest":      _fmt(earliest_ms),
        "latest":        _fmt(latest_ms),
    }
