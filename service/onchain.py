"""
service/onchain.py
鏈上數據服務 — TVL、穩定幣市值、資金費率歷史
透過 data_manager 本地緩存 + Binance API 補救 (新增 Bybit / OKX 極速備援)

[Task #1] SSL 繞過：所有外部 safe_get() 加入 verify=False
[Task #2] 非同步抓取：_fetch_funding_rate_history 改用 httpx.AsyncClient 並行發送請求
[Task #3] Geo-block 備援：Binance 遭遇 451 時，直接抓取 Bybit/OKX 最新數據，避開舊時間戳限制
"""
import time
import asyncio
import logging
import requests
from core.http_client import safe_get, safe_post
import urllib3          
import httpx            
import pandas as pd
import streamlit as st
from datetime import datetime

import service.historical_data_manager as data_manager

# 從集中設定檔讀取 SSL 動態驗證旗標
from config import SSL_VERIFY

logger = logging.getLogger(__name__)

# [Task #1] 動態 SSL：本地開發環境才關閉警告
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@st.cache_data(ttl=3600)
def fetch_aux_history():
    """
    獲取輔助歷史數據:
    - BTC 鏈上 TVL (DeFiLlama)
    - 全球穩定幣市值 (DeFiLlama)
    - BTC 資金費率歷史 (Binance / Bybit / OKX)
    返回: (tvl_df, stable_df, funding_df)
    """
    tvl = pd.DataFrame()
    stable = pd.DataFrame()
    funding = pd.DataFrame()

    # 優先透過 data_manager 本地緩存
    try:
        tvl, stable, funding = data_manager.load_all_historical_data()
    except Exception:
        pass

    # 補救：穩定幣市值
    if stable is None or stable.empty:
        stable = _fetch_stablecoin_history()

    # 補救：資金費率（非同步抓取）
    if funding is None or funding.empty:
        funding = _fetch_funding_rate_history()
        # [C-17] ccxt 路徑目前所有環境皆無法使用（SSL 攔截 / Streamlit Cloud 451），
        # 持久化寫回 SQLite 快取，避免每次啟動都要重新對外抓取
        if funding is not None and not funding.empty:
            try:
                data_manager._df_to_sqlite(funding, 'funding_history')
            except Exception as e:
                logger.warning(f"[Funding] 寫回 SQLite 快取失敗: {e}")

    return _clean(tvl, "tvl"), _clean(stable, "stable"), _clean(funding, "funding")

def _fetch_stablecoin_history():
    """
    同步補救：直接抓取全量穩定幣歷史市值。
    [Task #1] verify=False 繞過企業 SSL 憑證阻擋。
    """
    try:
        r = safe_get(
            "https://stablecoins.llama.fi/stablecoincharts/all",
            timeout=10,
            verify=SSL_VERIFY,  
        )
        if r.status_code != 200:
            return pd.DataFrame()
        recs = []
        for item in r.json():
            try:
                dt = pd.to_datetime(int(item['date']), unit='s', utc=True)
                mc = float(item['totalCirculating']['peggedUSD'])
                recs.append({'date': dt, 'mcap': mc})
            except Exception:
                continue
        if recs:
            return pd.DataFrame(recs).set_index('date')
    except Exception as e:
        logger.warning(f"Stablecoin fetch error: {e}")
    return pd.DataFrame()

# ──────────────────────────────────────────────────────────────────────────────
# [Task #2] 非同步資金費率抓取 (加入 Bybit / OKX 備援)
# ──────────────────────────────────────────────────────────────────────────────

async def _fetch_binance_funding_page_async(client: httpx.AsyncClient, start_ts: int) -> list:
    """抓取 Binance 單頁資金費率"""
    try:
        resp = await client.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={'symbol': 'BTCUSDT', 'limit': 1000, 'startTime': start_ts},
            timeout=10.0
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        pass # 失敗交由外層 fallback 處理
    return []

async def _fetch_binance_funding_rate_async() -> pd.DataFrame:
    """非同步抓取 Binance 全部資金費率"""
    start_ts = int(datetime(2021, 1, 1).timestamp() * 1000)
    end_ts   = int(datetime.now().timestamp() * 1000)
    interval_ms = 1000 * 8 * 3600 * 1000  

    page_starts = []
    ts = start_ts
    while ts < end_ts:
        page_starts.append(ts)
        ts += interval_ms  

    all_rates = []
    async with httpx.AsyncClient(verify=SSL_VERIFY) as client:
        tasks = [_fetch_binance_funding_page_async(client, s) for s in page_starts]
        pages = await asyncio.gather(*tasks, return_exceptions=True)

        for page in pages:
            if isinstance(page, list):
                all_rates.extend(page)

    if not all_rates:
        return pd.DataFrame()

    recs = []
    for item in all_rates:
        try:
            dt   = pd.to_datetime(int(item['fundingTime']), unit='ms', utc=True)
            rate = float(item['fundingRate']) * 100  
            recs.append({'date': dt, 'fundingRate': rate})
        except Exception:
            continue

    if not recs:
        return pd.DataFrame()

    df = pd.DataFrame(recs).set_index('date')
    df = df[~df.index.duplicated(keep='first')]
    df.sort_index(inplace=True)
    logger.info(f"[Market] 成功使用 Binance 抓取資金費率歷史: {len(df)} 筆")
    return df

async def _fetch_fallback_funding_rate_async() -> pd.DataFrame:
    """
    極速備援機制：當 Binance 遭遇 451 封鎖時觸發。
    放棄抓取 2021 歷史（避免舊時間戳報錯），改為只抓取最新數據（完全滿足推播需求）。
    """
    recs = []
    source = ""
    
    async with httpx.AsyncClient(verify=SSL_VERIFY) as client:
        # 第一備援：Bybit (抓取最新 200 筆，約 66 天，不押時間範圍)
        try:
            resp = await client.get(
                "https://api.bybit.com/v5/market/funding/history",
                params={'category': 'linear', 'symbol': 'BTCUSDT', 'limit': 200},
                timeout=10.0
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("retCode") == 0:
                    bybit_list = data.get("result", {}).get("list", [])
                    for item in bybit_list:
                        try:
                            dt = pd.to_datetime(int(item['fundingRateTimestamp']), unit='ms', utc=True)
                            rate = float(item['fundingRate']) * 100  
                            recs.append({'date': dt, 'fundingRate': rate})
                        except Exception:
                            continue
                    source = "Bybit"
        except Exception as e:
            logger.warning(f"[Bybit] 備援請求失敗: {e}")

        # 第二備援：如果 Bybit 失敗，嘗試 OKX (抓取最新 100 筆)
        if not recs:
            logger.warning("[Market] Bybit 失敗，切換 OKX 資金費率備援機制...")
            try:
                resp = await client.get(
                    "https://www.okx.com/api/v5/public/funding-rate-history",
                    params={'instId': 'BTC-USDT-SWAP', 'limit': 100},
                    timeout=10.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == "0":
                        okx_list = data.get("data", [])
                        for item in okx_list:
                            try:
                                dt = pd.to_datetime(int(item['fundingTime']), unit='ms', utc=True)
                                rate = float(item['fundingRate']) * 100  
                                recs.append({'date': dt, 'fundingRate': rate})
                            except Exception:
                                continue
                        source = "OKX"
            except Exception as e:
                logger.warning(f"[OKX] 備援請求失敗: {e}")

    if not recs:
        logger.error("[Market] ❌ 所有資金費率來源 (Binance/Bybit/OKX) 均抓取失敗")
        return pd.DataFrame()

    df = pd.DataFrame(recs).set_index('date')
    df = df[~df.index.duplicated(keep='first')]
    df.sort_index(inplace=True)
    logger.info(f"[Market] 成功使用 {source} 抓取最新資金費率: {len(df)} 筆")
    return df

async def _fetch_funding_rate_async() -> pd.DataFrame:
    """資金費率主邏輯：優先 Binance，失敗則 fallback 到極速備援"""
    df = await _fetch_binance_funding_rate_async()
    if not df.empty:
        return df
    
    logger.warning("[Market] Binance 資金費率抓取失敗 (可能遇到 451 封鎖)，啟動備援機制...")
    return await _fetch_fallback_funding_rate_async()

def _fetch_funding_rate_history() -> pd.DataFrame:
    """
    公開同步介面：包裝非同步函式，讓現有的同步呼叫端不需改動。
    """
    try:
        return asyncio.run(_fetch_funding_rate_async())
    except RuntimeError as e:
        if "cannot be called from a running event loop" not in str(e):
            raise
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _fetch_funding_rate_async())
            try:
                return future.result(timeout=60)
            except Exception as exc:
                logger.warning(f"Async funding rate fallback error: {exc}")
                return pd.DataFrame()

def _clean(df, name="data"):
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        if df.index.dtype == 'object' or str(df.index.dtype) == 'string':
            df.index = pd.to_datetime(df.index, format='mixed', utc=True)
        else:
            df.index = pd.to_datetime(df.index, utc=True)
        df = df[df.index.notna()]
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        logger.warning(f"Error cleaning {name}: {e}")
        return pd.DataFrame()
