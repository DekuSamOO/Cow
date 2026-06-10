"""
service/market_snapshot.py
每日市場快照 — 自建合約/情緒歷史（OI 總量、BTC.D、資金費率、價格）

背景：
  相對高點（逃頂）判斷需要「OI 是否處於相對高 / 創近期新高」與「BTC.D 是否下降
  （山寨輪動）」，但：
    - Binance OI 歷史端點僅保留約 30 天 → 拿不到真實「歷史新高」
    - CoinGecko /global 只給當前 BTC.D，無免費歷史
  因此本模組每日落地一筆快照到 db/market_snapshot.json，長期累積出真實歷史，
  供 core/relative_high 計算 OI 分位 / BTC.D 趨勢。

  OI 加總沿用 Crypto/BTC_WATCH.py 的算法：
    U本位(fapi BTCUSDT，單位已是 BTC) + 幣本位(dapi BTCUSD_PERP，張數×$100/價)。

純資料層，無 Streamlit 依賴（dashboard 與每日 script 共用）。
"""
import os
import json
import time
import logging
from datetime import datetime, timezone

import urllib3

from config import SSL_VERIFY
from core.http_client import safe_get

logger = logging.getLogger(__name__)

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CACHE = os.path.join(os.path.dirname(__file__), "..", "db", "market_snapshot.json")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def _load() -> dict:
    try:
        with open(_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logger.warning(f"[market_snapshot] 寫入失敗：{e}")


# ──────────────────────────────────────────────────────────────────────────────
# 即時抓取（單筆）
# ──────────────────────────────────────────────────────────────────────────────

def fetch_total_oi(price: float = None) -> dict:
    """
    抓 U本位 + 幣本位加總 OI（顆 BTC）。沿用 BTC_WATCH.py 算法。
    回傳 {oi_btc, oi_usd, price, u_oi, coin_oi_btc}；失敗欄位為 None。
    """
    out = {"oi_btc": None, "oi_usd": None, "price": price,
           "u_oi": None, "coin_oi_btc": None}
    try:
        if price is None:
            rp = safe_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                          timeout=8, verify=SSL_VERIFY, headers=_UA)
            if rp.status_code == 200:
                price = float(rp.json()["price"])
                out["price"] = price

        ru = safe_get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
                      timeout=8, verify=SSL_VERIFY, headers=_UA)
        u_oi = float(ru.json()["openInterest"]) if ru.status_code == 200 else None

        coin_btc = None
        rc = safe_get("https://dapi.binance.com/dapi/v1/openInterest?symbol=BTCUSD_PERP",
                      timeout=8, verify=SSL_VERIFY, headers=_UA)
        if rc.status_code == 200 and price:
            contracts = float(rc.json()["openInterest"])
            coin_btc = (contracts * 100) / price   # 幣本位每張面值 $100

        out["u_oi"] = u_oi
        out["coin_oi_btc"] = coin_btc
        if u_oi is not None:
            total = u_oi + (coin_btc or 0.0)
            out["oi_btc"] = total
            if price:
                out["oi_usd"] = total * price
    except Exception as e:
        logger.warning(f"[market_snapshot] OI 抓取失敗：{e}")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 每日快照寫入（每日 script 呼叫一次）
# ──────────────────────────────────────────────────────────────────────────────

def append_daily_snapshot(price: float = None, funding_rate: float = None,
                          btc_dominance: float = None) -> dict:
    """
    記錄當日一筆快照（同日重複呼叫會覆蓋當日）。
    OI 自行抓取；funding_rate / btc_dominance 可由呼叫端傳入（避免重複請求），
    未傳則本函式不另抓（保持單一職責，由呼叫端決定）。

    回傳寫入的該筆快照 dict。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    oi = fetch_total_oi(price=price)
    snap = {
        "price":         oi.get("price") if price is None else price,
        "oi_btc":        oi.get("oi_btc"),
        "oi_usd":        oi.get("oi_usd"),
        "funding_rate":  funding_rate,
        "btc_dominance": btc_dominance,
        "ts":            time.time(),
    }
    store = _load()
    store[today] = snap
    _save(store)
    logger.info(f"[market_snapshot] {today} 已記錄 OI={snap['oi_btc']} BTC.D={btc_dominance}")
    return snap


# ──────────────────────────────────────────────────────────────────────────────
# 歷史讀取與統計
# ──────────────────────────────────────────────────────────────────────────────

def backfill_oi_history(days: int = 30) -> int:
    """
    用 Binance openInterestHist（fapi U本位 + dapi 幣本位，皆保留近 ~30 天每日）回補
    market_snapshot.json 中缺漏的日期——例如週末關機沒跑、或首次啟用時一次補滿近 30 天。

    只填「不存在」的日期，不覆蓋既有 live 快照，也不碰今日（今日交給 append_daily_snapshot）。
    回補口徑與 live fetch_total_oi 一致（已交叉驗證）：
      oi_btc = U sumOpenInterest(BTC) + COIN sumOpenInterestValue(BTC)
    funding_rate / btc_dominance 留 None（無免費回溯源），source 標 'backfill'。
    回傳新增筆數。
    """
    store = _load()
    existing = {k for k in store if not k.startswith("_")}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        ru = safe_get("https://fapi.binance.com/futures/data/openInterestHist",
                      params={"symbol": "BTCUSDT", "period": "1d", "limit": days},
                      timeout=10, verify=SSL_VERIFY, headers=_UA)
        rc = safe_get("https://dapi.binance.com/futures/data/openInterestHist",
                      params={"pair": "BTCUSD", "contractType": "PERPETUAL", "period": "1d", "limit": days},
                      timeout=10, verify=SSL_VERIFY, headers=_UA)
        u_by, c_by = {}, {}
        for r in ru.json():
            d = datetime.fromtimestamp(r["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            u_by[d] = float(r["sumOpenInterest"])
        for r in rc.json():
            d = datetime.fromtimestamp(r["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            c_by[d] = float(r["sumOpenInterestValue"])   # 幣本位 OI（單位 BTC）
    except Exception as e:
        logger.warning(f"[market_snapshot] backfill 抓取失敗：{e}")
        return 0

    filled = 0
    for d, u_oi in u_by.items():
        if d in existing or d == today:
            continue
        store[d] = {"price": None, "oi_btc": u_oi + c_by.get(d, 0.0), "oi_usd": None,
                    "funding_rate": None, "btc_dominance": None,
                    "source": "backfill", "ts": time.time()}
        filled += 1
    if filled:
        _save(store)
        logger.info(f"[market_snapshot] backfill 補了 {filled} 天 OI 歷史")
    return filled


def get_snapshot_history() -> dict:
    """回傳全部快照 {date_str: snap}（已排除 meta 鍵）。"""
    return {k: v for k, v in _load().items() if not k.startswith("_")}


def get_snapshot_staleness_days():
    """
    最近一筆快照距今天數（0=今天有快照）；無資料回 None。
    供每日推播健康檢查：本機 OI 快照排程靜默失敗時，讓使用者從 LINE 卡片看到警告。
    """
    hist = get_snapshot_history()
    if not hist:
        return None
    try:
        last = max(datetime.strptime(d, "%Y-%m-%d").date() for d in hist)
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - last).days


def _series(field: str):
    """回傳依日期升冪排序的數值序列（呼叫端只需值，不需日期）。"""
    hist = get_snapshot_history()
    pairs = sorted((d, s.get(field)) for d, s in hist.items() if s.get(field) is not None)
    return [v for _, v in pairs]


def get_oi_stats(current_oi: float = None, window: int = 90) -> dict:
    """
    回傳 OI 相對位置統計：
      {n, percentile, is_near_high, current, max_window, days}
    - percentile：current_oi 在近 window 日 OI 分布的百分位（0-100）
    - is_near_high：是否 ≥ 近 window 日的 95 分位（近期新高）
    - n：可用歷史筆數（不足時 percentile=None，呼叫端據此降權/標示「累積中」）
    """
    vals = _series("oi_btc")
    if window and len(vals) > window:
        vals = vals[-window:]
    n = len(vals)
    out = {"n": n, "percentile": None, "is_near_high": False,
           "current": current_oi, "max_window": (max(vals) if vals else None),
           "days": n}
    if current_oi is None or n < 10:
        return out   # 樣本太少：不下結論
    below = sum(1 for v in vals if v <= current_oi)
    pct = below / n * 100
    out["percentile"] = pct
    out["is_near_high"] = pct >= 95.0 or (out["max_window"] and current_oi >= out["max_window"])
    return out


def get_btcd_trend(window: int = 30) -> dict:
    """
    BTC.D 趨勢：{n, current, change_pp, is_falling}
    change_pp = 最新 − window 前（百分點）；is_falling=True 代表資金流出 BTC（山寨輪動）。
    樣本不足回 n<2、其餘 None。
    """
    vals = _series("btc_dominance")
    n = len(vals)
    out = {"n": n, "current": (vals[-1] if vals else None),
           "change_pp": None, "is_falling": False}
    if n < 2:
        return out
    ref = vals[-window] if n > window else vals[0]
    out["change_pp"] = vals[-1] - ref
    out["is_falling"] = out["change_pp"] <= -1.0   # 30 日內 BTC.D 跌 ≥1pp
    return out
