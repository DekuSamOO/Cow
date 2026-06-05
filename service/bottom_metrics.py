"""
service/bottom_metrics.py
鏈上底部錨指標服務 — Realized Price / Balanced Price / CVDD / MVRV-Z Score
─────────────────────────────────────────────────────────────────────
資料源：bitcoin-data.com（免費；約 4 年日線歷史；HTTP 429 會限流）
策略：
  1. 逐端點抓取，端點間 sleep 節流避開 429
  2. 成功結果落地 db/bottom_metrics_cache.json（跨 Streamlit cold start / 429 續用）
  3. 任一端點失敗 → 回退該端點的快取值；全失敗 → 回 None（呼叫端 graceful）

用途：core/bottom_floors 取各指標「最新值」當底部地板/錨點參考。
純資料層，無 Streamlit 依賴（dashboard 與 LINE script 共用）。
"""
import json
import os
import time
import logging
import urllib3
import pandas as pd

from config import SSL_VERIFY
from core.http_client import safe_get

logger = logging.getLogger(__name__)

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BASE  = "https://bitcoin-data.com/v1"
_CACHE = os.path.join(os.path.dirname(__file__), "..", "db", "bottom_metrics_cache.json")
_UA    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# metric_key -> (endpoint, json value field)
_ENDPOINTS = {
    "realized_price": ("realized-price", "realizedPrice"),
    "balanced_price": ("balanced-price", "balancedPrice"),
    "cvdd":           ("cvdd",           "cvdd"),
    "mvrv_zscore":    ("mvrv-zscore",    "mvrvZscore"),
}

_THROTTLE_SEC      = 4.0    # 端點間隔，避開 bitcoin-data.com burst 限流
_RATE_LIMIT_WAIT   = 20.0   # 遇 429 的長退避
_MAX_429_RETRY     = 3
_CACHE_MAX_AGE_H   = 12


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    except Exception as e:
        logger.warning(f"[bottom_metrics] 快取寫入失敗（{path}）：{e}")


def _fetch_one(endpoint: str, val_key: str) -> dict:
    """抓單一端點 → {date_str: value}。
    改用 core.http_client.safe_get（統一 UA／重試）；backoff_factor 設為 429 長退避秒數，
    對限流／連線失敗做 20→40→80s 指數退避，最終失敗拋例外交由外層回退快取。"""
    url = f"{_BASE}/{endpoint}"
    r = safe_get(url, headers=_UA, timeout=25,
                 retries=_MAX_429_RETRY, backoff_factor=_RATE_LIMIT_WAIT,
                 verify=SSL_VERIFY)
    out = {}
    for row in r.json():
        d = row.get("d")
        v = row.get(val_key)
        if d and v is not None:
            try:
                out[d] = float(v)
            except (TypeError, ValueError):
                continue
    if not out:
        raise ValueError(f"{endpoint} 回傳空資料")
    return out


def fetch_bottom_metrics(force: bool = False) -> dict:
    """
    回傳 {metric_key: {date_str: value}}（含 latest helper 由呼叫端取用）。
    新鮮快取（< _CACHE_MAX_AGE_H 小時）直接用，不打 API；force=True 強制重抓。
    """
    cache = _load_json(_CACHE)
    fresh = (
        not force
        and cache.get("_ts")
        and (time.time() - cache["_ts"]) < _CACHE_MAX_AGE_H * 3600
        and all(k in cache for k in _ENDPOINTS)
    )
    if fresh:
        return {k: cache[k] for k in _ENDPOINTS}

    result = {}
    updated = False
    for i, (key, (ep, vk)) in enumerate(_ENDPOINTS.items()):
        if i:
            time.sleep(_THROTTLE_SEC)
        try:
            result[key] = _fetch_one(ep, vk)
            updated = True
        except Exception as e:
            logger.warning(f"[bottom_metrics] {ep} 抓取失敗（{type(e).__name__}），回退快取")
            if key in cache:
                result[key] = cache[key]

    if updated:
        cache.update(result)
        cache["_ts"] = time.time()
        _save_json(_CACHE, cache)

    return result


def get_latest_bottom_metrics(force: bool = False) -> dict:
    """
    回傳各指標最新一筆值（float）：
      {realized_price, balanced_price, cvdd, mvrv_zscore, asof}
    缺漏的指標為 None。
    """
    raw = fetch_bottom_metrics(force=force)
    out = {k: None for k in _ENDPOINTS}
    asof = None
    for key, series in raw.items():
        if not series:
            continue
        last_d = max(series.keys())
        out[key] = series[last_d]
        if asof is None or last_d > asof:
            asof = last_d
    out["asof"] = asof
    return out


_HASHRATE_URL   = "https://api.blockchain.info/charts/hash-rate"
_HASHRATE_CACHE = os.path.join(os.path.dirname(__file__), "..", "db", "hashrate_history.json")


def fetch_hashrate_history_ths(force: bool = False) -> dict:
    """
    全網算力歷史 {date_str: hashrate_TH/s}（blockchain.info charts，全期日線）。
    含 12h 持久化快取；失敗回退快取。供礦工成本歷史重建。
    """
    cache = _load_json(_HASHRATE_CACHE)
    if (not force and cache.get("_ts")
            and (time.time() - cache["_ts"]) < _CACHE_MAX_AGE_H * 3600
            and cache.get("data")):
        return cache["data"]

    try:
        r = safe_get(
            _HASHRATE_URL,
            params={"timespan": "all", "format": "json", "sampled": "true"},
            headers=_UA, timeout=30, verify=SSL_VERIFY,
        )
        from datetime import datetime as _dt, timezone as _tz
        data = {}
        for pt in r.json().get("values", []):
            d = _dt.fromtimestamp(pt["x"], tz=_tz.utc).strftime("%Y-%m-%d")
            data[d] = float(pt["y"])   # 已是 TH/s
        if not data:
            raise ValueError("hash-rate 回傳空資料")
        _save_json(_HASHRATE_CACHE, {"_ts": time.time(), "data": data})
        return data
    except Exception as e:
        logger.warning(f"[bottom_metrics] 算力歷史抓取失敗（{type(e).__name__}），回退快取")
        return cache.get("data", {})


def to_frame(metric_key: str, force: bool = False) -> pd.DataFrame:
    """單一指標轉 DataFrame（index=date naive, 欄=value），供回測/疊圖。"""
    raw = fetch_bottom_metrics(force=force)
    series = raw.get(metric_key) or {}
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(
        sorted(series.items()), columns=["date", metric_key]
    )
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")
