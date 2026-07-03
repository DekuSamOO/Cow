"""
service/macro_data.py  ·  v1.1
宏觀經濟數據服務 — 全球流動性 M2、日圓匯率、美國 CPI、量子威脅等級

版次記錄:
  v1.0  初版（FRED 公開 CSV API + Yahoo Finance + 靜態量子威脅評估）
  v1.1  [本次] 全面加入靜態備援機制（fallback），解決 Level 3 宏觀視角數據載入
        失敗時顯示空白「—」或折線圖靜默消失的問題：
        ① fetch_m2_series()  — 原本失敗回傳空 DataFrame，UI 折線圖靜默消失
          → 改為回傳含最近已知值的單點 DataFrame（is_fallback=True）
        ② fetch_usdjpy()     — 原本 Yahoo+FRED 雙層均失敗時 rate=None，UI 顯示「—」
          → 新增第三層靜態備援值，is_fallback=True
        ③ fetch_us_cpi_yoy() — 原本失敗 yoy_pct=None，UI 顯示「—」
          → 改為回傳靜態備援值，is_fallback=True
        ④ 所有備援回傳物件帶 is_fallback=True 旗標
          → UI 層顯示值 + ⚠️(備援) 標記，讓用戶明確知道為靜態數據
        ⑤ DataFrame 型備援帶 .fallback_note 屬性（供 tooltip 說明備援原因）
        ⑥ 新增 _FALLBACK 字典，集中管理四項靜態備援值
          → 每月只需人工更新一次（修改 value + date 兩欄）

數據源（全部免費、無需 API Key）:
  - 美國 M2 週頻: FRED 公開 CSV API (WM2NS)
  - 日圓匯率:     Yahoo Finance (USDJPY=X) → FRED (DEXJPUS) → 靜態備援  [v1.1]
  - 美國 CPI:     FRED 公開 CSV API (CPIAUCSL) → 靜態備援               [v1.1]
  - 量子威脅:     靜態評估（無即時 API，基於公開量子計算里程碑）

FRED 公開 CSV API 說明:
  端點: https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES_ID}
  - 無需 API Key，直接 GET 訪問
  - WM2NS   : 美國 M2 貨幣供應量（週頻，十億美元）
  - CPIAUCSL: 美國城市消費者物價指數（月頻，季節調整）
  - DEXJPUS : 美元兌日圓（日頻）
"""
import io
import math
import logging
import requests
from core.http_client import safe_get, safe_post

logger = logging.getLogger(__name__)
import pandas as pd
import numpy as np
import yfinance as yf
import streamlit as st

from config import SSL_VERIFY

# 不需要 API Key 的 FRED 公開 CSV 端點
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"

# ──────────────────────────────────────────────────────────────────────────────
# [v1.1 新增] 靜態備援數據字典
#
# 用途  : 所有對外 API 失敗時的最後防線，確保 UI 永遠有值可顯示
# 更新  : 每月人工更新一次；更新後修改對應的 value + date 兩欄即可
# 最後更新: 2025-02-25
# ──────────────────────────────────────────────────────────────────────────────
_FALLBACK = {
    # DXY 美元指數（tab_bull_radar Level 3 DXY 相關性使用）
    "dxy": {
        "value": 106.5,
        "date":  "2025-02-21",
        "note":  "DXY 美元指數（靜態備援，Yahoo Finance 連線失敗）",
    },
    # 美國 M2 貨幣供應量（FRED WM2NS，十億美元）
    "m2": {
        "value": 21450.0,
        "date":  "2025-01-01",
        "note":  "美國 M2（FRED WM2NS 靜態備援，FRED 連線失敗）",
    },
    # 美國 CPI YoY 年增率（FRED CPIAUCSL，%）
    "cpi": {
        "value": 3.0,
        "date":  "2025-01-01",
        "note":  "美國 CPI YoY（FRED CPIAUCSL 靜態備援，FRED 連線失敗）",
    },
    # USD/JPY 日圓匯率
    "usdjpy": {
        "value": 150.5,
        "date":  "2025-02-21",
        "note":  "USD/JPY 日圓匯率（靜態備援，Yahoo Finance + FRED 均失敗）",
    },
    # 美國 PCE 物價指數 YoY 年增率（FRED PCEPI，%）
    "pce": {
        "value": 2.5,
        "date":  "2026-04-01",
        "note":  "美國 PCE YoY（FRED PCEPI 靜態備援，FRED 連線失敗）",
    },
    # 美國非農就業總人數（FRED PAYEMS，千人）
    "nfp": {
        "value": 159001.0,
        "date":  "2026-05-01",
        "note":  "美國非農就業（FRED PAYEMS 靜態備援，FRED 連線失敗）",
    },
    # 美國失業率（FRED UNRATE，%）
    "unrate": {
        "value": 4.3,
        "date":  "2026-05-01",
        "note":  "美國失業率（FRED UNRATE 靜態備援，FRED 連線失敗）",
    },
    # BTC 市值佔比（CoinGecko /global，%）
    "btc_dominance": {
        "value": 56.0,
        "date":  "2026-06-08",
        "note":  "BTC 市值佔比（CoinGecko /global 靜態備援，連線失敗）",
    },
}


@st.cache_data(ttl=600, show_spinner=False)
def _fred_reachable() -> bool:
    """
    FRED 可達性探針（快取 10 分鐘）。企業 proxy 常完全卡住 FRED，導致每個序列
    timeout 30s×3=90s、多序列累計數分鐘卡死 dashboard。先用單次短探測（12s, retries=0）
    判斷；不通則所有 _fred_fetch 立即拋例外走備援（本機快速結束）。
    雲端 FRED 正常回應 <2s，探針通過，不影響真實抓取。
    """
    try:
        safe_get(_FRED_CSV.format(sid="UNRATE"), timeout=12, retries=0, verify=SSL_VERIFY)
        return True
    except Exception:
        logger.warning("[FRED] 可達性探針失敗（疑似企業 proxy 阻擋），本 session 改走靜態備援")
        return False


def _fred_fetch(series_id: str, timeout: int = 30) -> pd.DataFrame:
    if not _fred_reachable():
        raise RuntimeError(f"FRED 不可達（circuit open），略過 {series_id} 直接走備援")
    """
    從 FRED 公開 CSV 端點抓取時間序列，返回 DatetimeIndex DataFrame。
    FRED 以 '.' 代表缺失值，需先替換為 NaN。
    若請求失敗直接拋出例外，由各呼叫方的 try/except 處理備援邏輯。
    """
    url  = _FRED_CSV.format(sid=series_id)
    resp = safe_get(url, timeout=timeout, verify=SSL_VERIFY)
    resp.raise_for_status()
    # FRED 已將 CSV 首欄表頭由 "DATE" 改為 "observation_date"（2024+）。
    # 不再寫死欄名，改取「第一欄」當日期欄，避免表頭一變就靜默走備援。
    df = pd.read_csv(io.StringIO(resp.text), na_values=["."])
    if df.empty or df.shape[1] < 2:
        raise ValueError(f"FRED {series_id} 回傳格式異常")
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).set_index(date_col)
    df.index.name = "DATE"
    # 強制轉為數字（FRED 偶爾回傳帶空白字元的字串）
    df = df.apply(pd.to_numeric, errors="coerce").dropna()
    return df


# [v1.1 新增] ─────────────────────────────────────────────────────────────────
def _make_fallback_df(key: str, col: str) -> pd.DataFrame:
    """
    從 _FALLBACK 字典建立單點備援 DataFrame，並附加識別屬性。

    Parameters
    ----------
    key : _FALLBACK 的鍵（"m2" / "dxy" 等）
    col : 欄位名稱（"m2_billions" / "close" 等）

    附加屬性（供 UI 層判斷與顯示）:
      df.is_fallback   = True  → UI 顯示 ⚠️(備援) 標記
      df.fallback_note = str   → 說明備援日期與原因（供 tooltip）
    """
    fb = _FALLBACK[key]
    df = pd.DataFrame(
        {col: [fb["value"]]},
        index=pd.DatetimeIndex([pd.Timestamp(fb["date"])]),
    )
    df.is_fallback   = True
    df.fallback_note = fb["note"]
    return df


# ──────────────────────────────────────────────────────────────────────────────
# M2 貨幣供應量
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400)  # M2 為週頻數據，每天快取一次即可
def fetch_m2_series() -> pd.DataFrame:
    """
    抓取美國 M2 貨幣供應量週頻歷史序列（FRED WM2NS）。
    作為全球流動性代理指標（美元為世界儲備貨幣，與全球 M2 相關性最高）。

    返回:
        pd.DataFrame  index=DATE（週頻）, columns=['m2_billions']
        單位: 十億美元（Billions of USD, Seasonally Adjusted）

    [v1.1] 失敗處理變更:
      v1.0: return pd.DataFrame(columns=["m2_billions"])
            → 空 DF，UI 折線圖靜默消失，用戶不知道出了什麼問題
      v1.1: return _make_fallback_df("m2", "m2_billions")
            → 單點備援 DF（is_fallback=True），UI 改顯示 metric 卡片 + ⚠️(備援)
      UI 判斷: getattr(df, 'is_fallback', False)
    """
    try:
        df = _fred_fetch("WM2NS")
        df.columns     = ["m2_billions"]
        df.is_fallback = False  # 明確標記：非備援（供 UI 判斷）
        return df
    except Exception as e:
        logger.warning(f"[M2] FRED WM2NS 抓取失敗: {e}，使用靜態備援值")
        return _make_fallback_df("m2", "m2_billions")


# ──────────────────────────────────────────────────────────────────────────────
# 日圓匯率
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3_600)  # 匯率每小時刷新
def fetch_usdjpy() -> dict:
    """
    抓取當前 USD/JPY 匯率。

    三層備援（優先序）:
      1. Yahoo Finance USDJPY=X（即時，延遲 < 15 分鐘）
      2. FRED DEXJPUS（日頻，T+1 延遲，無地理封鎖）          ← v1.0 已有
      3. _FALLBACK 靜態值（最後防線，附日期標記）              ← [v1.1 新增]

    返回 dict:
        rate        : float  當前匯率（日圓/美元）
        change_pct  : float  日變化率（%）
        prev_close  : float  前收盤價
        trend       : str    '日圓貶值 (USD↑)' | '日圓升值 (USD↓)' | '橫盤'
        source      : str    數據來源標籤
        is_fallback : bool   是否為靜態備援值  ← [v1.1 新增]

    [v1.1] 第三層靜態備援:
      v1.0: return {"rate": None, "trend": "N/A", "source": "失敗"}
            → UI 收到 rate=None 顯示空白「—」
      v1.1: return _FALLBACK["usdjpy"] 靜態值（is_fallback=True）
            → UI 顯示備援值 + ⚠️(備援) 標記
    """
    # ── 第一層：Yahoo Finance ────────────────────────────────────────────────
    try:
        hist = yf.download("USDJPY=X", period="5d", progress=False, auto_adjust=True)
        if hist.empty or len(hist) < 2:
            raise ValueError("Yahoo Finance USDJPY=X 無資料")

        # 處理 MultiIndex columns（yfinance 新版可能回傳 MultiIndex）
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)
        hist.columns = [c.lower() for c in hist.columns]

        latest = float(hist["close"].iloc[-1])
        prev   = float(hist["close"].iloc[-2])
        change = (latest / prev - 1) * 100

        return {
            "rate":        latest,
            "prev_close":  prev,
            "change_pct":  change,
            "trend":       "日圓貶值 (USD↑)" if change >  0.05 else (
                           "日圓升值 (USD↓)" if change < -0.05 else "橫盤"),
            "source":      "Yahoo Finance",
            "is_fallback": False,  # [v1.1] 明確標記非備援
        }
    except Exception as e:
        logger.warning(f"[JPY] Yahoo Finance 抓取失敗: {e}")

    # ── 第二層：FRED DEXJPUS（日頻，無地理封鎖）─────────────────────────────
    try:
        df = _fred_fetch("DEXJPUS")
        df.columns = ["jpy"]
        latest = float(df["jpy"].iloc[-1])
        prev   = float(df["jpy"].iloc[-2]) if len(df) >= 2 else latest
        change = (latest / prev - 1) * 100

        return {
            "rate":        latest,
            "prev_close":  prev,
            "change_pct":  change,
            "trend":       "日圓貶值 (USD↑)" if change >  0.05 else (
                           "日圓升值 (USD↓)" if change < -0.05 else "橫盤"),
            "source":      "FRED DEXJPUS（T+1）",
            "is_fallback": False,  # [v1.1] 明確標記非備援
        }
    except Exception as e:
        logger.warning(f"[JPY] FRED DEXJPUS 也失敗: {e}")

    # ── 第三層：靜態備援 ─────────────────────────────────────────────────────
    # [v1.1 新增] v1.0 在這裡直接 return {"rate": None, ...}
    # 改為回傳最近已知靜態值，UI 顯示值 + ⚠️(備援) 標記
    fb = _FALLBACK["usdjpy"]
    logger.info(f"[JPY] 使用靜態備援值 {fb['value']} ({fb['date']})")
    return {
        "rate":        fb["value"],
        "prev_close":  fb["value"],
        "change_pct":  0.0,
        "trend":       f"⚠️ 靜態備援值（{fb['date']}）",
        "source":      "靜態備援",
        "is_fallback": True,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 美國 CPI
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400)  # CPI 為月頻，每天快取一次
def fetch_us_cpi_yoy() -> dict:
    """
    抓取美國 CPI 年增率（FRED CPIAUCSL，月頻季節調整）。

    YoY 計算: (當月 CPI - 去年同月 CPI) / 去年同月 CPI × 100

    返回 dict:
        yoy_pct     : float  最新 CPI YoY（%）
        latest_date : str    最新數據月份（格式 "YYYY-MM"）
        mom_pct     : float  環比（月增率 %）
        trend       : str    '通膨升溫 ↑' | '通膨降溫 ↓' | '穩定 →'
        source      : str    'FRED CPIAUCSL'
        is_fallback : bool   是否為靜態備援值  ← [v1.1 新增]

    [v1.1] 失敗處理變更:
      v1.0: return {"yoy_pct": None, "source": "失敗"}
            → UI 收到 yoy_pct=None 顯示空白「—」
      v1.1: return _FALLBACK["cpi"] 靜態值（is_fallback=True）
            → UI 顯示備援值 + ⚠️(備援) 標記
    """
    try:
        df = _fred_fetch("CPIAUCSL")
        df.columns = ["cpi"]
        df = df.sort_index()

        # YoY: 與去年同月比較（月頻 pct_change(12) = 12個月前）
        yoy = df["cpi"].pct_change(12) * 100
        mom = df["cpi"].pct_change(1)  * 100  # 月增率

        yoy_curr = float(yoy.iloc[-1])
        yoy_prev = float(yoy.iloc[-2]) if len(yoy) >= 2 else yoy_curr
        mom_curr = float(mom.iloc[-1])

        # 判斷趨勢：連續兩個月 YoY 變化方向
        if yoy_curr > yoy_prev + 0.15:
            trend = "通膨升溫 ↑"
        elif yoy_curr < yoy_prev - 0.15:
            trend = "通膨降溫 ↓"
        else:
            trend = "穩定 →"

        return {
            "yoy_pct":     yoy_curr,
            "mom_pct":     mom_curr,
            "latest_date": df.index[-1].strftime("%Y-%m"),
            "trend":       trend,
            "source":      "FRED CPIAUCSL",
            "is_fallback": False,  # [v1.1] 明確標記非備援
        }
    except Exception as e:
        logger.warning(f"[CPI] FRED CPIAUCSL 抓取失敗: {e}，使用靜態備援值")

        # [v1.1 新增] v1.0 在這裡 return {"yoy_pct": None, ...}
        # 改為回傳靜態備援值，UI 顯示值 + ⚠️(備援) 標記
        fb = _FALLBACK["cpi"]
        return {
            "yoy_pct":     fb["value"],
            "mom_pct":     None,
            "latest_date": fb["date"],
            "trend":       f"⚠️ 靜態備援值（{fb['date']}）",
            "source":      "靜態備援",
            "is_fallback": True,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 美國 PCE 物價指數 YoY（聯準會最重視的通膨指標）
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400)  # PCE 月頻，每天快取一次
def fetch_us_pce_yoy() -> dict:
    """
    美國 PCE 物價指數年增率（FRED PCEPI，月頻）。
    PCE 是聯準會貨幣政策最重視的通膨指標（較 CPI 更被 Fed 採用）。

    返回 dict: {yoy_pct, mom_pct, latest_date, trend, source, is_fallback}
    失敗回靜態備援（is_fallback=True），與 CPI 同邏輯。
    """
    try:
        df = _fred_fetch("PCEPI")
        df.columns = ["pce"]
        df = df.sort_index()
        yoy = df["pce"].pct_change(12) * 100
        mom = df["pce"].pct_change(1)  * 100
        yoy_curr = float(yoy.iloc[-1])
        yoy_prev = float(yoy.iloc[-2]) if len(yoy) >= 2 else yoy_curr
        if yoy_curr > yoy_prev + 0.15:
            trend = "通膨升溫 ↑"
        elif yoy_curr < yoy_prev - 0.15:
            trend = "通膨降溫 ↓"
        else:
            trend = "穩定 →"
        return {
            "yoy_pct":     yoy_curr,
            "mom_pct":     float(mom.iloc[-1]),
            "latest_date": df.index[-1].strftime("%Y-%m"),
            "trend":       trend,
            "source":      "FRED PCEPI",
            "is_fallback": False,
        }
    except Exception as e:
        logger.warning(f"[PCE] FRED PCEPI 抓取失敗: {e}，使用靜態備援值")
        fb = _FALLBACK["pce"]
        return {
            "yoy_pct":     fb["value"],
            "mom_pct":     None,
            "latest_date": fb["date"][:7],
            "trend":       f"⚠️ 靜態備援值（{fb['date']}）",
            "source":      "靜態備援",
            "is_fallback": True,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 美國非農就業 + 失業率（就業類數據：決定經濟是否生病）
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86_400)
def fetch_nfp() -> dict:
    """
    美國非農就業總人數（FRED PAYEMS，千人，月頻）。
    回傳最新月「新增就業」= 當月總數 − 前月總數（千人），及趨勢。

    就業太強（新增大、失業低）→ Fed 不急著降息 → BTC 承壓；
    就業熄火 → 衰退預期 → 降息救市 → 中長期對 BTC 利多（見來源筆記第五段）。

    返回 dict: {change_k, level_k, latest_date, trend, source, is_fallback}
    """
    try:
        df = _fred_fetch("PAYEMS")
        df.columns = ["payems"]
        df = df.sort_index()
        level = float(df["payems"].iloc[-1])
        prev  = float(df["payems"].iloc[-2]) if len(df) >= 2 else level
        change_k = level - prev   # 當月新增就業（千人）
        return {
            "change_k":    change_k,
            "level_k":     level,
            "latest_date": df.index[-1].strftime("%Y-%m"),
            "trend":       "就業強勁 (升息傾向)" if change_k > 150 else (
                           "就業疲弱 (降息傾向)" if change_k < 50 else "溫和增長"),
            "source":      "FRED PAYEMS",
            "is_fallback": False,
        }
    except Exception as e:
        logger.warning(f"[NFP] FRED PAYEMS 抓取失敗: {e}，使用靜態備援值")
        fb = _FALLBACK["nfp"]
        return {
            "change_k":    None,
            "level_k":     fb["value"],
            "latest_date": fb["date"][:7],
            "trend":       f"⚠️ 靜態備援值（{fb['date']}）",
            "source":      "靜態備援",
            "is_fallback": True,
        }


@st.cache_data(ttl=86_400)
def fetch_unrate() -> dict:
    """
    美國失業率（FRED UNRATE，%，月頻）。

    返回 dict: {value, prev, change_pp, latest_date, trend, source, is_fallback}
    失業率飆高 → 經濟生病 → 降息救火（中長期 BTC 利多）。
    """
    try:
        df = _fred_fetch("UNRATE")
        df.columns = ["unrate"]
        df = df.sort_index()
        val  = float(df["unrate"].iloc[-1])
        prev = float(df["unrate"].iloc[-2]) if len(df) >= 2 else val
        change = val - prev
        return {
            "value":       val,
            "prev":        prev,
            "change_pp":   change,
            "latest_date": df.index[-1].strftime("%Y-%m"),
            "trend":       "失業上升 (經濟轉弱)" if change > 0.05 else (
                           "失業下降 (經濟強勁)" if change < -0.05 else "持平"),
            "source":      "FRED UNRATE",
            "is_fallback": False,
        }
    except Exception as e:
        logger.warning(f"[UNRATE] FRED UNRATE 抓取失敗: {e}，使用靜態備援值")
        fb = _FALLBACK["unrate"]
        return {
            "value":       fb["value"],
            "prev":        fb["value"],
            "change_pp":   0.0,
            "latest_date": fb["date"][:7],
            "trend":       f"⚠️ 靜態備援值（{fb['date']}）",
            "source":      "靜態備援",
            "is_fallback": True,
        }


# ──────────────────────────────────────────────────────────────────────────────
# BTC 市值佔比（Bitcoin Dominance）— 山寨輪動/資金末端訊號
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3_600)
def fetch_btc_dominance() -> dict:
    """
    BTC 市值佔比（CoinGecko /global，%）。
    BTC.D 下降而總市值維持 = 資金輪動到山寨（牛市末端訊號，見來源筆記第四段）。
    僅回傳當前值；趨勢由 service/market_snapshot 的每日快照累積後計算。

    返回 dict: {value, source, is_fallback}
    """
    try:
        r = safe_get("https://api.coingecko.com/api/v3/global",
                     timeout=8, verify=SSL_VERIFY)
        if r.status_code == 200:
            btc_d = r.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
            if btc_d is not None:
                return {"value": float(btc_d), "source": "CoinGecko", "is_fallback": False}
        raise ValueError(f"CoinGecko /global status={r.status_code}")
    except Exception as e:
        logger.warning(f"[BTC.D] CoinGecko 抓取失敗: {e}，使用靜態備援值")
        fb = _FALLBACK["btc_dominance"]
        return {"value": fb["value"], "source": "靜態備援", "is_fallback": True}


# ──────────────────────────────────────────────────────────────────────────────
# 總經事件行事曆（FOMC/CPI/PCE/非農）— 本地鏡像，與 Notion「BTC 總經事件」DB 同源
# ──────────────────────────────────────────────────────────────────────────────

# 函式本體已搬到 service/macro_events.py（零重依賴，不 import streamlit/yfinance，
# 見該檔頭說明——watcher.py／daily_line_notify.py 改直接吃那支輕量模組）。這裡 re-export
# 只為了不改動 handler/tab_macro_compass.py 既有的 `from service.macro_data import
# ...get_next_macro_event` 呼叫路徑（dashboard 本就需要 streamlit，不受影響）。
from service.macro_events import get_next_macro_event, _MACRO_EVENTS_PATH  # noqa: F401


# ──────────────────────────────────────────────────────────────────────────────
# 量子威脅等級（靜態評估，每季人工更新）
# ──────────────────────────────────────────────────────────────────────────────

def get_quantum_threat_level() -> dict:
    """
    量子計算對比特幣威脅等級的靜態評估。

    威脅模型:
    - 比特幣使用 secp256k1 橢圓曲線加密，256 位元密鑰
    - Shor 演算法可破解，所需量子資源估算:
        ~400 萬實體量子位元（容錯邏輯位元: ~4,000）
    - 現況 (2026): IBM Heron r2 = 156 物理位元，Google Willow = 105 物理位元
    - 容錯量子電腦距成熟仍需 10-20 年

    NIST PQC 參考: https://csrc.nist.gov/projects/post-quantum-cryptography
    (NIST 後量子密碼標準已於 2024 年正式發布 ML-KEM/ML-DSA/SLH-DSA)

    返回 dict:
        level     : str  威脅等級文字
        level_num : int  1-5（1=最低）
        color     : str  顯示顏色（hex）
        status    : str  簡短狀態
        desc      : str  詳細說明
        year_est  : str  預估威脅成熟年份
        ref_url   : str  參考連結
        updated   : str  本評估更新時間  ← [v1.1 補齊此欄位]
    """
    # 2026 年評估: Level 1 (Very Low)
    # 現有最佳量子電腦距破解 Bitcoin 仍有 3-4 個數量級的差距
    return {
        "level":     "極低",          # 縮短文字避免 st.metric 截斷
        "level_num": 1,
        "color":     "#00ff88",
        "status":    "🟢 目前無威脅 (Level 1/5)",
        "desc": (
            "Google Willow: 105 物理量子位元｜IBM Heron r2: 156 位元\n"
            "破解 secp256k1 需 ~400 萬容錯實體位元，差距 4 個數量級\n"
            "NIST PQC 標準已於 2024 正式發布 (ML-KEM / ML-DSA)"
        ),
        "year_est": "2035–2045+",
        "ref_url":  "https://csrc.nist.gov/projects/post-quantum-cryptography",
        "updated":  "2026-Q1 靜態評估",
    }