"""
service/funding_history.py · BTC 永續資金費率歷史（CSV 增量快取）

用途：提供 PiT（point-in-time）滾動分位所需的「過去 N 日年化資費」序列。
core/relative_low 的負費率子項 2026-08 改為「絕對階梯 ∪ 滾動分位」混合計分後，
評分當下需要一段歷史；core 維持純函數零網路，抓取與快取一律由本模組負責。

單位慣例（與 service/onchain._fetch_binance_funding_rate_async 一致，勿再乘一次 100）：
  CSV 欄位 fundingRate = **每 8h 費率的百分比**（API 原始小數 × 100），例如 0.01 表示 0.01%/8h。
  年化 = 每日均值 × 3 × 365（每日結算 3 次），與 core.relative_high.annualize_funding 同式。

離線行為：抓不到就用現有 CSV（best-effort），不拋例外、不讓監控畫面掛掉。
"""
import os
import time
import logging

import pandas as pd

logger = logging.getLogger(__name__)

_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(_COW, "db", "cache", "funding_rate_history.csv")
FAPI_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
HISTORY_START = "2019-01-01"      # 幣安 BTCUSDT 永續資費史實際自 2019-09-10 起
_MIN_REFRESH_SEC = 3600           # 同進程內最短重抓間隔

_mem_cache = {"ts": 0.0, "series": None}


def _read_cache() -> pd.DataFrame:
    if not os.path.exists(CACHE_PATH):
        return pd.DataFrame(columns=["date", "fundingRate"])
    try:
        df = pd.read_csv(CACHE_PATH, parse_dates=["date"])
        return df.dropna().drop_duplicates("date").sort_values("date")
    except Exception as e:
        logger.warning("資費快取讀取失敗（%s）→ 視為空", e)
        return pd.DataFrame(columns=["date", "fundingRate"])


def _fetch_since(start_ms: int, symbol: str = "BTCUSDT") -> pd.DataFrame:
    """自 start_ms 起分頁抓取；任何失敗回空表（呼叫端退回既有快取）。"""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        return pd.DataFrame(columns=["date", "fundingRate"])

    rows, cur = [], start_ms
    s = requests.Session()
    s.verify = False          # 公司網路為 SSL 攔截（見 _governance/ENV-notes.md），非防火牆封鎖
    try:
        while True:
            r = s.get(FAPI_URL, params={"symbol": symbol, "startTime": cur, "limit": 1000},
                      timeout=30).json()
            if not isinstance(r, list) or not r:
                break
            rows += r
            if len(r) < 1000:
                break
            cur = int(r[-1]["fundingTime"]) + 1
            time.sleep(0.15)
    except Exception as e:
        logger.warning("資費歷史抓取中斷（%s）→ 用已取得的 %d 筆", e, len(rows))

    if not rows:
        return pd.DataFrame(columns=["date", "fundingRate"])
    df = pd.DataFrame({
        "date": pd.to_datetime([int(x["fundingTime"]) for x in rows], unit="ms"),
        "fundingRate": [float(x["fundingRate"]) * 100 for x in rows],   # → %/8h
    })
    return df.drop_duplicates("date").sort_values("date")


def refresh_cache(symbol: str = "BTCUSDT") -> pd.DataFrame:
    """增量更新 CSV 快取並回傳全量（8h 粒度、%/8h）。"""
    old = _read_cache()
    if old.empty:
        start_ms = int(pd.Timestamp(HISTORY_START).timestamp() * 1000)
    else:
        start_ms = int(old["date"].iloc[-1].timestamp() * 1000) + 1
    new = _fetch_since(start_ms, symbol)
    if new.empty:
        return old
    df = pd.concat([old, new]).drop_duplicates("date").sort_values("date")
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        df.to_csv(CACHE_PATH, index=False)
    except Exception as e:
        logger.warning("資費快取寫入失敗（%s）→ 僅本次記憶體可用", e)
    return df


def load_funding_ann_daily(refresh: bool = True, symbol: str = "BTCUSDT") -> pd.Series:
    """
    回傳「每日年化資費（%）」序列，index 為日期。
    refresh=False 純讀快取（測試/回測用，零網路）。
    """
    now = time.time()
    if refresh and (now - _mem_cache["ts"]) < _MIN_REFRESH_SEC and _mem_cache["series"] is not None:
        return _mem_cache["series"]
    df = refresh_cache(symbol) if refresh else _read_cache()
    if df.empty:
        return pd.Series(dtype=float)
    s = df.set_index("date")["fundingRate"].resample("D").mean().dropna() * 3 * 365
    if refresh:
        _mem_cache["ts"] = now
        _mem_cache["series"] = s
    return s


def funding_ann_hist(window: int = 400, refresh: bool = True, symbol: str = "BTCUSDT") -> list:
    """
    給 core.relative_low 用的純量序列：最近 window 日的年化資費（含今日、不含未來）。
    抓不到資料回空 list → core 端自動退回純絕對階梯（行為同 2026-08 之前）。
    """
    s = load_funding_ann_daily(refresh=refresh, symbol=symbol)
    if s.empty:
        return []
    return [float(v) for v in s.tail(window).values]


def funding_8h_daily_mean(refresh: bool = True, symbol: str = "BTCUSDT"):
    """
    最新一日的**日均**資費（%/8h）——給計分用，與校準口徑一致。

    2026-08-25 獨立檢核 🟠 No.6：校準與稽核都用 `resample('D').mean()` 的日均年化，
    但 BTC_WATCH.get_funding_rate() 回的是單筆 lastFundingRate，
    單筆年化的波動比日均大 **1.15 倍**（新環境 std 5.04 vs 4.38、最低 -16.62% vs -11.06%）
    → 生產值更常落在分布尾端，子項實際觸發率會**高於**校準值。
    面板顯示仍用即時單筆（那才是「現在的費率」），**計分改吃這個**。
    取不到回 None → 呼叫端退回即時值（行為同 2026-08 之前）。
    """
    s = load_funding_ann_daily(refresh=refresh, symbol=symbol)
    if s.empty:
        return None
    return float(s.iloc[-1]) / (3 * 365)
