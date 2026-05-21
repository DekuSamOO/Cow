"""
service/market_data.py
市場數據服務 — BTC 歷史 OHLCV + DXY
增量更新：本地 CSV 緩存，只下載缺失日期
加入 SSL 繞過機制與多層備援 API:
  0th: 本地 SQLite DB（db/btcusdt_15m_*.db，由 collector 預先收集）— 最優先、最可靠
  1st: Yahoo Finance (yfinance)
  2nd: Binance REST API（直接 HTTP，不依賴 ccxt）— 部分 Streamlit Cloud IP 被 451 封鎖
  3rd: Kraken 公開 API — 無地理限制，適合 Streamlit Cloud/GitHub Actions
  4th: CryptoCompare 公開 API — 有 2010 年起完整 BTC 日線歷史，分頁抓取
"""
import os
import time
import pandas as pd
import yfinance as yf
import streamlit as st
from datetime import datetime, timedelta, timezone
import requests
from core.http_client import safe_get, safe_post
import urllib3

# 從集中設定檔讀取 SSL 動態驗證旗標
from config import SSL_VERIFY

# 本地 SQLite DB 讀取（由 collector/btc_price_collector.py 生成）
from service.local_db_reader import has_local_data, read_btc_daily

# 動態 SSL：本地開發環境才關閉警告；雲端 SSL_VERIFY=True 維持正常驗證
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BTC_CSV = "db/cache/BTC_HISTORY.csv"

# Binance REST API 端點（不需要 API Key，公開 klines）
_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def get_yf_session():
    """
    建立自訂的 requests Session 供 yfinance 使用。
    1. verify=SSL_VERIFY: 動態 SSL 驗證（本地 False，雲端 True）。
    2. 加入 User-Agent: 降低在 Streamlit Cloud 上被 Yahoo 視為機器人阻擋的機率。
    """
    session = requests.Session()
    session.verify = SSL_VERIFY  # 動態 SSL：本地 False / 雲端 True
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return session


def fetch_binance_daily(start_date_str):
    """
    第二備援：直接呼叫 Binance REST API（不使用 ccxt），抓取 BTC/USDT 日線 K 棒。

    API: GET https://api.binance.com/api/v3/klines
    - 無需 API Key（公開端點）
    - limit 最大 1000，分頁以 startTime 推進
    - 注意：Binance 對部分 Streamlit Cloud 或 GitHub Actions IP 返回 451（地理封鎖）
    """
    start_ts = int(datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    now_ts   = int(datetime.now(timezone.utc).timestamp() * 1000)

    all_klines = []
    since = start_ts

    while since < now_ts:
        try:
            resp = safe_get(
                _BINANCE_KLINES_URL,
                params={
                    "symbol":    "BTCUSDT",
                    "interval":  "1d",
                    "startTime": since,
                    "limit":     1000,
                },
                timeout=15,
                verify=SSL_VERIFY,
            )
            # 451 地理封鎖：直接拋出讓 caller 捕捉
            if resp.status_code == 451:
                raise requests.HTTPError(f"451 Binance geo-block")
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[Binance REST] 請求失敗 (since={since}): {e}")
            raise  # 由外層 try/except 處理並切換備援

        if not batch:
            break

        all_klines.extend(batch)

        if len(batch) < 1000:
            break  # 已到最新資料

        # 下一頁起點：最後一根 K 棒的 open_time + 1 天（ms）
        since = int(batch[-1][0]) + 86_400_000
        time.sleep(0.05)  # 遵守 Binance 限速（1200 req/min）

    if not all_klines:
        return pd.DataFrame()

    # 取前 6 欄：open_time, open, high, low, close, volume
    df = pd.DataFrame(all_klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "ct", "qa", "nt", "tb", "tq", "i",
    ])
    df["date"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
    df.set_index("date", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    return df


def fetch_kraken_daily(start_date_str):
    """
    第三備援：從 Kraken 公開 API 抓取 BTC/USD 日線數據。
    Kraken 無地理封鎖限制，適合從 Streamlit Cloud / GitHub Actions 呼叫。
    """
    start_ts = int(datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    url = "https://api.kraken.com/0/public/OHLC"
    all_candles = []
    since = start_ts

    for _ in range(15):  # 每頁最多 720 筆，15 頁可涵蓋約 30 年
        try:
            resp = safe_get(
                url,
                params={'pair': 'XBTUSD', 'interval': 1440, 'since': since},
                timeout=15,
                verify=SSL_VERIFY,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get('error'):
                print(f"[Kraken] API 錯誤: {data['error']}")
                break

            candles = data.get('result', {}).get('XXBTZUSD', [])
            if not candles:
                break

            all_candles.extend(candles)

            # 'last' 是下一頁的 since 值（已存在的最後一筆 Unix 時間戳）
            last_ts = data.get('result', {}).get('last', 0)
            if last_ts <= since:
                break  # 無更多數據
            since = last_ts
            time.sleep(0.5)  # 遵守 Kraken 速率限制（每秒 1 請求）

        except Exception as e:
            print(f"[Kraken] 分頁請求失敗: {e}")
            break

    if not all_candles:
        return pd.DataFrame()

    # Kraken OHLC 格式: [time, open, high, low, close, vwap, volume, count]
    df = pd.DataFrame(
        all_candles,
        columns=['timestamp', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'count']
    )
    df['date'] = pd.to_datetime(df['timestamp'].astype(int), unit='s')
    df.set_index('date', inplace=True)
    df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    return df

def fetch_cryptocompare_daily(start_date_str):
    """
    第四備援：從 CryptoCompare 公開 API 抓取 BTC/USD 日線歷史。
    - 無需 API Key，免費端點
    - 分頁倒序抓取（toTs 往前推）
    """
    url = "https://min-api.cryptocompare.com/data/v2/histoday"
    start_ts = int(datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    now_ts = int(datetime.now(timezone.utc).timestamp())

    all_rows = []
    to_ts = now_ts  # 從當前時間往前翻頁

    for _ in range(20):  # 最多 20 頁 × 2000 = 40,000 天，足以覆蓋 BTC 全歷史
        try:
            resp = safe_get(
                url,
                params={
                    "fsym": "BTC",
                    "tsym": "USD",
                    "limit": 2000,
                    "toTs": to_ts,
                },
                timeout=20,
                verify=SSL_VERIFY,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("Response") != "Success":
                print(f"[CryptoCompare] API 返回錯誤: {data.get('Message', '未知')}")
                break

            rows = data.get("Data", {}).get("Data", [])
            if not rows:
                break

            # 過濾掉 time=0 或 close=0 的無效行
            valid = [r for r in rows if r.get("time", 0) > 0 and r.get("close", 0) > 0]
            all_rows.extend(valid)

            # 最早一筆時間戳
            earliest_ts = min(r["time"] for r in valid)
            if earliest_ts <= start_ts:
                break  # 已覆蓋目標起始日期，不再繼續

            # 往更早翻頁（減 1 天避免重複）
            to_ts = earliest_ts - 86_400
            time.sleep(0.3)

        except Exception as e:
            print(f"[CryptoCompare] 分頁請求失敗: {e}")
            break

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("date", inplace=True)
    # 只保留 OHLCV 欄位，並轉為 float
    df = df[["open", "high", "low", "close", "volumeto"]].copy()
    df.rename(columns={"volumeto": "volume"}, inplace=True)
    df = df.astype(float)

    # 過濾到 start_date_str 之後的數據
    df = df[df.index >= pd.Timestamp(start_date_str)]
    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    return df


@st.cache_data(ttl=300)
def fetch_market_data():
    """
    獲取 BTC-USD 日線數據（本地 CSV 增量更新）
    返回: (btc_df, dxy_df)
    """
    today = datetime.now(timezone.utc).date()
    # 實例化帶有 SSL 繞過與偽裝的 Session
    session = get_yf_session()

    # 1. 讀取本地緩存
    if os.path.exists(BTC_CSV):
        try:
            local_df = pd.read_csv(BTC_CSV, index_col=0, parse_dates=True)
            if local_df.index.tz is not None:
                local_df.index = local_df.index.tz_localize(None)
            last_date = local_df.index[-1].date()
        except Exception:
            local_df = pd.DataFrame()
            last_date = None
    else:
        local_df = pd.DataFrame()
        last_date = None

    # 2. 確定需要下載的範圍
    start_fetch_date = None
    
    if last_date and last_date < today:
        start_fetch_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
    elif not last_date:
        start_fetch_date = "2015-01-01"

    # 若有需要更新的日期，開始抓取
    btc_new = pd.DataFrame()
    if start_fetch_date:
        from core.data_sources import build_btc_history_chain
        chain = build_btc_history_chain(session)
        result = chain.fetch(start_fetch_date)
        if result.data is not None and not result.data.empty:
            btc_new = result.data
            print(f"[Market] {result.source_used} 成功，取得 {len(btc_new)} 筆")

    # 3. 合併並存檔
    if not btc_new.empty:
        full_df = pd.concat([local_df, btc_new]) if not local_df.empty else btc_new
        full_df = full_df[~full_df.index.duplicated(keep='last')]
        full_df.sort_index(inplace=True)
        full_df.to_csv(BTC_CSV)
        btc_final = full_df
    else:
        btc_final = local_df

    # ── T 日數據縫合 (Data Stitching) ───────────────────────────────────────────
    # 【重大修正】：確保本地庫若落後多天，能從正確的「缺口日期」開始補齊，且移除會觸發 451 的幣安備援
    if not btc_final.empty and btc_final.index[-1].date() < today:
        # 計算從哪一天開始補資料 (從歷史最後一筆的隔天開始)
        gap_start_date = (btc_final.index[-1].date() + timedelta(days=1)).strftime('%Y-%m-%d')
        
        # 避免極端情況：如果 gap_start_date 不合理地大於今天，將其修正為今天
        if gap_start_date > today.strftime('%Y-%m-%d'):
            gap_start_date = today.strftime('%Y-%m-%d')

        _tday_df  = pd.DataFrame()
        
        # 縫合備援鏈
        from core.data_sources import build_btc_stitch_chain
        stitch_chain = build_btc_stitch_chain(session)
        stitch_result = stitch_chain.fetch(gap_start_date)
        
        if stitch_result.data is not None and not stitch_result.data.empty:
            _tday_df = stitch_result.data
            print(f"[Market] 縫合區塊：成功使用 {stitch_result.source_used} 補齊缺漏資料")

        # 縫合：把補齊的數據 Concat 到歷史末端，並寫回 CSV 避免下次重複抓取
        if not _tday_df.empty:
            if _tday_df.index.tz is not None:
                _tday_df.index = _tday_df.index.tz_localize(None)
            btc_final = pd.concat([btc_final, _tday_df])
            btc_final = btc_final[~btc_final.index.duplicated(keep='last')]
            btc_final.sort_index(inplace=True)
            btc_final.to_csv(BTC_CSV)

    if btc_final.empty:
        print("[Market] ❌ 五層備援均失敗（本地DB / Yahoo / Binance / Kraken / CryptoCompare）")
        return pd.DataFrame(), pd.DataFrame()

    # 4. DXY (美元指數)
    try:
        # DXY 同樣套用自訂 session 避開 SSL 問題
        dxy = yf.download("DX-Y.NYB", start="2017-01-01", interval="1d", progress=False, session=session)
        dxy.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in dxy.columns]
        if not dxy.empty and dxy.index.tz is not None:
            dxy.index = dxy.index.tz_localize(None)
    except Exception:
        dxy = pd.DataFrame()

    return btc_final, dxy
