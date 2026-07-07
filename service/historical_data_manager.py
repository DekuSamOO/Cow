"""
data_manager.py
歷史數據管理 — TVL、穩定幣市值、資金費率緩存

[Task #1] SSL 繞過：所有 safe_get() 加入 verify=False
[Task #3] 重試機制：TVL/穩定幣 API 加入指數退避重試 (max 3 次)
[Task #4] SQLite：將 CSV 緩存改為 SQLite，解決多執行緒寫入衝突
[Task #8] 環境變數：CCXT API Key 改由 .env 讀取
"""
import pandas as pd
import logging
import requests
from core.http_client import safe_get, safe_post

logger = logging.getLogger(__name__)
import urllib3          # [Task #1] SSL 警告靜默
import os
import time             # [Task #3] 指數退避 sleep
import sqlite3          # [Task #4] SQLite 資料庫連線
import threading        # [Task #4] 寫入鎖，防止多執行緒同時寫入
from datetime import datetime, timedelta, timezone
import asyncio
from dotenv import load_dotenv  # [Task #8] 從 .env 讀取環境變數

# 從集中設定檔讀取 SSL 動態驗證旗標
from config import SSL_VERIFY

# [Task #8] 載入 .env 檔案（若不存在則靜默跳過）
load_dotenv()

# [Task #1] 動態 SSL：本地開發環境才關閉警告；雲端 SSL_VERIFY=True 維持正常驗證
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# [Task #4] SQLite 資料庫路徑設定
DATA_DIR = "data"
DB_PATH  = os.path.join(DATA_DIR, "cow_history.db")

# [Task #4] 模組等級的寫入鎖，確保多執行緒環境下 SQLite 不會同時寫入
_db_lock = threading.Lock()

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)


# ──────────────────────────────────────────────────────────────────────────────
# [Task #3] 指數退避重試裝飾器
# ──────────────────────────────────────────────────────────────────────────────
def _retry_request(url: str, params: dict = None, max_retries: int = 3,
                   timeout: int = 10) -> requests.Response | None:
    """
    帶有指數退避的 GET 請求封裝。
    - max_retries: 最大重試次數（不含首次嘗試）
    - 退避間隔：1s → 2s → 4s（2^n 秒）
    - [Task #1] 所有請求使用 verify=False 繞過企業 SSL
    - [Task #3] 捕捉 Timeout / ConnectionError 並自動重試

    返回: requests.Response 物件，或 None（全部重試失敗）
    """
    for attempt in range(max_retries + 1):
        try:
            resp = safe_get(url, params=params, timeout=timeout, verify=SSL_VERIFY)
            resp.raise_for_status()   # 非 2xx 狀態碼拋出例外
            return resp
        except requests.exceptions.Timeout:
            logger.warning(f"[Retry {attempt+1}/{max_retries}] Timeout: {url}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[Retry {attempt+1}/{max_retries}] ConnError: {e}")
        except requests.exceptions.HTTPError as e:
            logger.warning(f"[Retry {attempt+1}/{max_retries}] HTTPError {e.response.status_code}: {url}")
        except Exception as e:
            logger.warning(f"[Retry {attempt+1}/{max_retries}] Unknown error: {e}")

        if attempt < max_retries:
            wait = 2 ** attempt  # 指數退避：1s, 2s, 4s
            logger.warning(f"  → 等待 {wait}s 後重試...")
            time.sleep(wait)

    logger.warning(f"[Failed] 所有重試均失敗: {url}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# [Task #4] SQLite 工具函式
# ──────────────────────────────────────────────────────────────────────────────
def _get_db_connection() -> sqlite3.Connection:
    """
    建立並返回 SQLite 連線。
    使用 check_same_thread=False 允許多執行緒存取，
    但寫入操作須搭配 _db_lock 確保安全。
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # 啟用 WAL 模式：允許同時多讀單寫，大幅降低鎖定衝突
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _df_to_sqlite(df: pd.DataFrame, table_name: str) -> None:
    """
    將 DataFrame 寫入 SQLite 表格（取代 CSV）。
    - 使用 _db_lock 確保同一時間只有一個執行緒在寫入
    - if_exists='replace' 全量覆寫（適合 DeFiLlama 這種全量回傳的 API）
    """
    if df is None or df.empty:
        return
    with _db_lock:  # [Task #4] 寫入鎖，防止 Streamlit 多執行緒同時寫入損毀
        conn = _get_db_connection()
        try:
            # 將 index（通常是 datetime）重置為普通欄位再存入
            df_to_save = df.reset_index()
            df_to_save.to_sql(table_name, conn, if_exists='replace', index=False)
        finally:
            conn.close()


_VALID_TABLES = frozenset({'tvl_history', 'stablecoin_history', 'funding_history', 'btc_history'})


def _df_from_sqlite(table_name: str, index_col: str = 'date') -> pd.DataFrame:
    """
    從 SQLite 表格讀取 DataFrame。
    - 若表格不存在（首次啟動）回傳空 DataFrame
    - index_col 預設為 'date'，讀取後自動轉為 DatetimeIndex
    - 欄位名稱統一轉小寫，相容 yfinance 存入的 'Date'（大寫）
    """
    if table_name not in _VALID_TABLES:
        raise ValueError(f"[SQLite] 不允許的表格名稱: {table_name!r}，允許清單: {_VALID_TABLES}")
    try:
        conn = _get_db_connection()
        try:
            # 確認表格是否存在，避免 SQL 錯誤
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            if cursor.fetchone() is None:
                return pd.DataFrame()  # 首次啟動，表格尚未建立

            df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
            # [C-17] 只正規化 index 欄位的大小寫（相容 yfinance 'Date' vs 'date'），
            # 其餘資料欄位（如 fundingRate）保留寫入時的原始大小寫，避免與
            # update_funding_history() 寫入的 camelCase 產生大小寫不一致的欄位分裂
            if index_col not in df.columns:
                for c in df.columns:
                    if c.lower() == index_col.lower():
                        df.rename(columns={c: index_col}, inplace=True)
                        break
            if index_col in df.columns:
                df[index_col] = pd.to_datetime(df[index_col])
                df.set_index(index_col, inplace=True)
            return df
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[SQLite] 讀取 {table_name} 失敗: {e}")
        return pd.DataFrame()

# --- 1. DeFiLlama TVL History ---
def update_tvl_history() -> pd.DataFrame:
    """
    從 DeFiLlama 抓取 Bitcoin 鏈上 TVL 歷史，並緩存至 SQLite。

    [Task #1] verify=False 繞過企業 SSL（透過 _retry_request 統一處理）
    [Task #3] 最多重試 3 次，指數退避（1s, 2s, 4s）
    [Task #4] 改用 SQLite 存取，取代 TVL_HISTORY.csv

    DeFiLlama 的 historicalChainTvl 端點回傳全量數據（約幾十KB），
    直接全量覆寫即可，無需增量邏輯。
    """
    url = "https://api.llama.fi/v2/historicalChainTvl/Bitcoin"

    # [Task #3] 使用帶重試的請求函式
    resp = _retry_request(url, timeout=10)

    if resp is not None:
        try:
            data    = resp.json()
            new_df  = pd.DataFrame(data)
            # DeFiLlama 返回 Unix 時間戳（秒），轉為 UTC datetime 再去除時區
            new_df['date'] = pd.to_datetime(new_df['date'], unit='s', utc=True)
            new_df['date'] = new_df['date'].dt.tz_localize(None)
            new_df.set_index('date', inplace=True)

            # [Task #4] 寫入 SQLite，取代 CSV
            _df_to_sqlite(new_df, 'tvl_history')
            logger.info(f"[TVL] 已更新 {len(new_df)} 筆至 SQLite")
            return new_df
        except Exception as e:
            logger.warning(f"[TVL] 解析失敗: {e}")

    # 所有重試失敗 → 從 SQLite 讀取舊緩存（降級模式）
    cached = _df_from_sqlite('tvl_history')
    if not cached.empty:
        logger.warning("[TVL] 使用 SQLite 緩存數據（降級模式）")
    return cached

# --- 2. Global Stablecoin Market Cap History ---
def update_stablecoin_history() -> pd.DataFrame:
    """
    從 DeFiLlama 抓取全球穩定幣市值歷史，並緩存至 SQLite。

    [Task #1] verify=False 繞過企業 SSL（透過 _retry_request 統一處理）
    [Task #3] 最多重試 3 次，指數退避
    [Task #4] 改用 SQLite 存取，取代 STABLECOIN_HISTORY.csv
    """
    url = "https://stablecoins.llama.fi/stablecoincharts/all"

    # [Task #3] 使用帶重試的請求函式
    resp = _retry_request(url, timeout=10)

    if resp is not None:
        try:
            data = resp.json()
            processed = []
            for item in data:
                ts         = int(item['date'])
                total_circ = item.get('totalCirculating', {})
                mcap       = total_circ.get('peggedUSD', total_circ.get('usd', 0))

                # 過濾塵埃值（< 1000 USD 通常是測試/錯誤數據）
                if mcap <= 1000:
                    continue

                # 使用 UTC 時間避免本地時區問題（utcfromtimestamp 已棄用）
                dt_obj = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                processed.append({'date': dt_obj, 'mcap': mcap})

            if not processed:
                logger.warning("[Stablecoin] 警告：無有效數據，檢查 API 回傳格式")

            new_df = pd.DataFrame(processed)
            if not new_df.empty:
                new_df.set_index('date', inplace=True)
                if new_df.index.tz is not None:
                    new_df.index = new_df.index.tz_localize(None)

                # [Task #4] 寫入 SQLite
                _df_to_sqlite(new_df, 'stablecoin_history')
                logger.info(f"[Stablecoin] 已更新 {len(new_df)} 筆至 SQLite")
                return new_df
        except Exception as e:
            logger.warning(f"[Stablecoin] 解析失敗: {e}")

    # 所有重試失敗 → 從 SQLite 讀取舊緩存
    cached = _df_from_sqlite('stablecoin_history')
    if not cached.empty:
        logger.warning("[Stablecoin] 使用 SQLite 緩存數據（降級模式）")
    return cached

# --- 3. Binance Funding Rate History (Incremental) ---
def update_funding_history(symbol: str = 'BTC/USDT', limit: int = 1000) -> pd.DataFrame:
    """
    增量抓取 BTC/USDT 資金費率歷史，並緩存至 SQLite。

    [Task #1] CCXT 底層使用 requests，無法直接 verify=False；
              但企業網路通常對 Binance Futures API 有特殊處理，
              若仍失敗可改用 service/onchain.py 的非同步抓取作為補救。
    [Task #3] CCXT 呼叫加入 try/except + 指數退避重試（最多 3 次）
    [Task #4] 改用 SQLite 增量存取，取代 FUNDING_HISTORY.csv

    增量邏輯：
    1. 從 SQLite 讀取現有數據，取最後一筆時間戳
    2. 只下載該時間戳之後的新數據
    3. 合併後寫回 SQLite（去重排序）
    """
    # [Task #4] 從 SQLite 讀取現有緩存（取代 CSV 讀取）
    existing_df = _df_from_sqlite('funding_history')
    since_ts    = None

    if not existing_df.empty:
        # 計算上次最新時間戳（+1ms 避免重複抓同一筆）
        last_dt  = existing_df.index[-1]
        since_ts = int(last_dt.timestamp() * 1000) + 1
        logger.info(f"[Funding] 增量模式，從 {last_dt} 開始抓取")
    else:
        logger.info("[Funding] 首次抓取，獲取最近 1000 筆")

    # [Task #8] 從環境變數讀取 API Key（若未設定則使用公開 API，不影響資金費率查詢）
    api_key    = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    try:
        import ccxt  # 延遲導入，避免 Python 3.13 相容性問題造成整個模組載入失敗
    except ImportError as e:
        logger.warning(f"[Funding] ccxt 導入失敗（Python 版本相容問題）: {e}，使用 SQLite 緩存")
        return existing_df

    exchange_cfg = {'options': {'defaultType': 'future'}}
    if api_key and api_secret:
        exchange_cfg['apiKey']  = api_key
        exchange_cfg['secret']  = api_secret

    exchange = ccxt.binance(exchange_cfg)

    # [Task #3] 帶指數退避的 CCXT 重試邏輯
    rates = None
    for attempt in range(3):  # 最多重試 3 次
        try:
            if since_ts:
                rates = exchange.fetch_funding_rate_history(
                    symbol, since=since_ts, limit=limit
                )
            else:
                rates = exchange.fetch_funding_rate_history(symbol, limit=limit)
            break  # 成功則跳出重試迴圈
        except ccxt.NetworkError as e:
            logger.warning(f"[Funding][Retry {attempt+1}/3] 網路錯誤: {e}")
        except ccxt.ExchangeError as e:
            logger.warning(f"[Funding][Retry {attempt+1}/3] 交易所錯誤: {e}")
            break  # 交易所錯誤（如 API 權限問題）不需重試
        except Exception as e:
            logger.warning(f"[Funding][Retry {attempt+1}/3] 未知錯誤: {e}")

        if attempt < 2:
            wait = 2 ** attempt  # 指數退避：1s, 2s
            logger.warning(f"  → 等待 {wait}s 後重試...")
            time.sleep(wait)

    if not rates:
        logger.warning("[Funding] 抓取失敗，使用 SQLite 緩存數據（降級模式）")
        return existing_df

    # 解析 CCXT 返回數據
    new_data = [
        {
            'date':        r['datetime'],           # ISO 字串
            'fundingRate': r['fundingRate'] * 100   # 轉為百分比
        }
        for r in rates
    ]

    fetched_df             = pd.DataFrame(new_data)
    fetched_df['date']     = pd.to_datetime(fetched_df['date'], utc=True)
    fetched_df['date']     = fetched_df['date'].dt.tz_localize(None)
    fetched_df.set_index('date', inplace=True)

    # 合併新舊數據
    if not existing_df.empty:
        full_df = pd.concat([existing_df, fetched_df])
        full_df = full_df[~full_df.index.duplicated(keep='last')]
        full_df.sort_index(inplace=True)
    else:
        full_df = fetched_df

    # [Task #4] 寫入 SQLite（取代 CSV 覆寫）
    _df_to_sqlite(full_df, 'funding_history')
    logger.info(f"[Funding] 已更新，SQLite 現有 {len(full_df)} 筆")
    return full_df


def load_all_historical_data():
    """主控函式：依序觸發各數據更新並返回 DataFrames。"""
    print("=" * 40)
    print("Updating Historical Data (SQLite mode)...")
    print("=" * 40)
    tvl     = update_tvl_history()
    stable  = update_stablecoin_history()
    funding = update_funding_history()
    return tvl, stable, funding
