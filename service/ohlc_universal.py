"""
service/ohlc_universal.py
通用 OHLC 資料層 — 讓 BTC_WATCH 的通用評分軸（趨勢方向＋技術指標）能服務任意標的。

單一真實來源：watcher.py 與 scripts/universal_watch_poc.py 共用本模組，杜絕兩邊抓法漂移。

資料源：Yahoo **v8 chart JSON**（query1.finance.yahoo.com/v8/finance/chart/<symbol>）。
選它而非 yfinance 套件的原因（實證踩坑）：yfinance 在公司/共享 IP 上被 Yahoo 的 crumb
認證限流（YFRateLimitError 429），且預設 curl_cffi 路徑撞公司 SSL 攔截。v8 chart 不走 crumb、
verify=False 可過公司 SSL、單端點即可取幣對 / 美股 / 台股日線。
"""
import re

import requests
import urllib3
import pandas as pd

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 顯示用的市場類別中文標籤
KIND_LABEL = {"crypto": "加密貨幣", "tw_stock": "台股", "us_stock": "美股"}


def classify_symbol(raw: str) -> dict:
    """
    自動判定輸入代號的市場類別並映射到各資料源 symbol。

    規則：純數字 4–6 碼 / 帶 .TW → 台股（補 .TW）；含 USDT/USD/-USD → 加密幣對；
    其餘英文字母 → 美股。BTC 各種寫法統一標記 is_btc=True（路由到完整 BitcoinMonitor）。

    回傳 {kind, display, yahoo, is_btc}；加密另含 {base, binance, coin}（幣安 U本位/幣本位 symbol）。
    """
    s = (raw or "").strip().upper()
    if not s:
        raise ValueError("代號不可為空")

    # 台股
    if s.endswith(".TW"):
        return {"kind": "tw_stock", "display": s[:-3], "yahoo": s, "is_btc": False}
    if re.fullmatch(r"\d{4,6}", s):
        return {"kind": "tw_stock", "display": s, "yahoo": f"{s}.TW", "is_btc": False}

    # 加密（BTC 各寫法 → 完整 BitcoinMonitor）
    if s in ("BTCUSDT", "BTC", "BTCUSD", "XBTUSD", "BTC-USD"):
        return _crypto_info("BTC", is_btc=True)
    if s.endswith("-USD"):
        return _crypto_info(s[:-4], is_btc=False)
    if s.endswith("USDT"):
        return _crypto_info(s[:-4], is_btc=False)
    if s.endswith("USD"):
        return _crypto_info(s[:-3], is_btc=False)

    # 其餘視為美股
    return {"kind": "us_stock", "display": s, "yahoo": s, "is_btc": False}


def _crypto_info(base: str, is_btc: bool) -> dict:
    """以幣別基礎（BTC/ETH/SOL）組出幣安 U本位/幣本位 symbol 與 Yahoo symbol。"""
    return {
        "kind": "crypto",
        "display": f"{base}USDT",
        "yahoo": f"{base}-USD",
        "base": base,
        "binance": f"{base}USDT",        # 幣安 U 本位永續（funding/OI/klines）
        "coin": f"{base}USD_PERP",       # 幣安幣本位永續（部分幣別才有，抓不到自動略過）
        "is_btc": is_btc,
    }


def _session() -> requests.Session:
    """配好公司 SSL 攔截環境與 UA 的 Yahoo 用 Session（fetch_ohlc / fetch_live_quote 共用）。"""
    s = requests.Session()
    s.verify = False  # 公司 SSL 攔截環境（見全域 CLAUDE.md）
    s.headers.update({"User-Agent": _UA})
    return s


def fetch_ohlc(yahoo_symbol: str, rng: str = "2y") -> pd.DataFrame:
    """
    Yahoo v8 chart JSON → 日線 OHLCV，欄位用 core 期望的 lowercase、index 去時區。
    同一函式吃 BTC-USD / ETH-USD / AAPL / NVDA / 2330.TW，與 Binance 無關。
    台股 `.TW`（上市）查無資料時自動改試 `.TWO`（上櫃）——classify 無法預知上市/上櫃。
    """
    s = _session()
    candidates = [yahoo_symbol]
    if yahoo_symbol.endswith(".TW"):
        candidates.append(yahoo_symbol[:-3] + ".TWO")   # 上市查無 → 試上櫃
    res, last_err = None, None
    for sym in candidates:
        try:
            r = s.get(_YF_CHART + sym, params={"range": rng, "interval": "1d"}, timeout=20)
            r.raise_for_status()
            result = r.json()["chart"]["result"]
        except Exception as e:  # noqa: BLE001 — 單一候選時於迴圈外 re-raise 保留原因
            result, last_err = None, e
        if result:
            res = result[0]
            break
    if res is None:
        raise RuntimeError(f"無資料：{yahoo_symbol}") from last_err
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"],
    }, index=pd.to_datetime(res["timestamp"], unit="s"))
    df = df[df["close"].notna()].copy()   # 停牌/缺資料的列剔除
    df.index = df.index.tz_localize(None)
    if df.empty:
        raise RuntimeError(f"無有效日線：{yahoo_symbol}")
    return df


def fetch_live_quote(yahoo_symbol: str) -> dict:
    """
    輕量即時報價（Yahoo v8 meta.regularMarketPrice）— 供股票盤中每 60s 更新現價，
    與每小時的日線+指標分離。盤中=即時成交價、盤後=收盤價（regularMarketTime 凍結在收盤）。
    回傳 {price, ts, prev_close, volume}；失敗回 {}（呼叫端退回日線收盤）。

    volume 取 meta.regularMarketVolume（實測台股/美股皆有此欄，同一次請求內、不加額外網路成本）。
    """
    try:
        r = _session().get(_YF_CHART + yahoo_symbol,
                           params={"range": "1d", "interval": "1d"}, timeout=10)
        r.raise_for_status()
        m = r.json()["chart"]["result"][0]["meta"]
        p = m.get("regularMarketPrice")
        if p is None:
            return {}
        return {"price": float(p), "ts": m.get("regularMarketTime"),
                "prev_close": m.get("previousClose") or m.get("chartPreviousClose"),
                "volume": m.get("regularMarketVolume")}
    except Exception:
        return {}


def _is_tw_trading_hours(now=None) -> bool:
    """粗略判斷此刻是否為台股交易時段（週一~五 09:00–13:30 台北時間）。
    不含國定假日行事曆（本模組零外部依賴），僅供「盤中 vs 已收盤」顯示分流，非交易依據。
    now 供測試注入（tz-naive/aware皆可，只取 weekday/time），預設 None 用當下台北時間。"""
    import datetime
    if now is None:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Asia/Taipei"))
    if now.weekday() >= 5:   # 週六日
        return False
    return datetime.time(9, 0) <= now.time() <= datetime.time(13, 30)


def live_quote_freshness(q: dict, is_tw: bool = False) -> dict:
    """
    解讀 fetch_live_quote 回傳的時效與漲跌（ts/prev_close 語義只有本模組知道，
    與 fetcher 同源避免 watcher / universal_watch_poc 兩邊重算）。
    回傳 {label, age_sec, chg_pct}；label 為盤中/已收盤狀態字串，chg_pct 缺 prev_close 時為 None。

    ⚠️ TWSE 免費源（Yahoo/Google 皆同）法定延遲約 20 分鐘，`regularMarketTime` 盤中age
    幾乎必然 >15 分鐘 → 若沿用「age<900s＝盤中」的美股門檻，台股盤中會被永遠誤判成「已收盤」
    （age 15–60 分鐘時顯示「已收盤（0h前）」，看起來像顛倒，其實是判斷依據錯——用 timestamp
    新舊猜是否收盤，對有法定延遲的市場不成立）。is_tw=True 時改用**當下是否為交易時段**
    （`_is_tw_trading_hours`）判斷，如實標「盤中（資料延遲）」而非「已收盤」。
    """
    import time as _time
    age = _time.time() - q["ts"] if q.get("ts") else float("inf")
    if is_tw:
        if _is_tw_trading_hours():
            label = f"🟡 盤中（資料延遲 {int(age // 60)}分，TWSE免費源限制）"
        elif age < 6 * 3600:
            label = f"⚪ 已收盤（{int(age // 3600)}h 前）"
        else:
            label = "⚪ 已收盤"
    elif age < 900:
        label = "🟢 盤中即時"
    elif age < 6 * 3600:
        label = f"⚪ 已收盤（{int(age // 3600)}h 前）"
    else:
        label = "⚪ 已收盤"
    chg = ((q["price"] / q["prev_close"] - 1) * 100) if q.get("prev_close") else None
    return {"label": label, "age_sec": age, "chg_pct": chg}
