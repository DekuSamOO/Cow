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
    自動判定輸入代號的市場類別並映射到 Yahoo v8 symbol。

    規則：純數字 4–6 碼 / 帶 .TW → 台股（補 .TW）；含 USDT/USD/-USD → 加密幣對；
    其餘英文字母 → 美股。BTC 各種寫法統一標記 is_btc=True（路由到完整 BitcoinMonitor）。

    回傳 {kind, display, yahoo, is_btc}。
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
        return {"kind": "crypto", "display": "BTCUSDT", "yahoo": "BTC-USD", "is_btc": True}
    if s.endswith("-USD"):
        return {"kind": "crypto", "display": s, "yahoo": s, "is_btc": False}
    if s.endswith("USDT"):
        return {"kind": "crypto", "display": s, "yahoo": f"{s[:-4]}-USD", "is_btc": False}
    if s.endswith("USD"):
        return {"kind": "crypto", "display": s, "yahoo": f"{s[:-3]}-USD", "is_btc": False}

    # 其餘視為美股
    return {"kind": "us_stock", "display": s, "yahoo": s, "is_btc": False}


def fetch_ohlc(yahoo_symbol: str, rng: str = "2y") -> pd.DataFrame:
    """
    Yahoo v8 chart JSON → 日線 OHLCV，欄位用 core 期望的 lowercase、index 去時區。
    同一函式吃 BTC-USD / ETH-USD / AAPL / NVDA / 2330.TW，與 Binance 無關。
    """
    s = requests.Session()
    s.verify = False  # 公司 SSL 攔截環境（見全域 CLAUDE.md）
    s.headers.update({"User-Agent": _UA})
    r = s.get(_YF_CHART + yahoo_symbol, params={"range": rng, "interval": "1d"}, timeout=20)
    r.raise_for_status()
    result = r.json()["chart"]["result"]
    if not result:
        raise RuntimeError(f"無資料：{yahoo_symbol}")
    res = result[0]
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
