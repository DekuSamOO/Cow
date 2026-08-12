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

    # 其餘視為美股（W-10：class share 代號如 BRK.B / BF.B，Yahoo 需 BRK-B / BF-B；
    # 已在上方排除 .TW 情境，此處剩下的 . 只會是這類美股寫法，可安全轉換）
    return {"kind": "us_stock", "display": s, "yahoo": s.replace(".", "-"), "is_btc": False}


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


_SESSION = None      # 模組級單例（W-6）：60s 輪詢重用連線，不每次重做 TCP+TLS 握手
_RESOLVED: dict = {}  # raw yahoo symbol → 實際有資料的候選（W-3）：上櫃股解析一次後不再打 .TW 404


def _session() -> requests.Session:
    """配好公司 SSL 攔截環境與 UA 的 Yahoo 用 Session（fetch_ohlc / fetch_live_quote 共用）。
    模組級 lazy 單例：watcher 每 60s 輪詢即時報價，逐次新建 Session 會重做 TLS 握手
    （公司 SSL 攔截環境握手更貴），重用即省。"""
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.verify = False  # 公司 SSL 攔截環境（見全域 CLAUDE.md）
        s.headers.update({"User-Agent": _UA})
        _SESSION = s
    return _SESSION


def _tw_candidates(yahoo_symbol: str) -> list:
    """台股 `.TW`（上市）查無資料時的備援候選（`.TWO` 上櫃）。classify_symbol 無法預知
    上市/上櫃，fetch_ohlc / fetch_live_quote 都需要同一套候選清單，抽出避免重複維護
    （曾因 fetch_live_quote 漏了這段，上櫃股「現價/即時成交量」永遠 404，見該函式說明）。
    已解析過的代號直接回單一候選（`_RESOLVED`）：上櫃股否則每次 60s 刷新都先吃一發
    注定 404 的 .TW 才試 .TWO。暫時性失敗不清快取（fetch 端自然重試）。"""
    resolved = _RESOLVED.get(yahoo_symbol)
    if resolved:
        return [resolved]
    candidates = [yahoo_symbol]
    if yahoo_symbol.endswith(".TW"):
        candidates.append(yahoo_symbol[:-3] + ".TWO")   # 上市查無 → 試上櫃
    return candidates


def fetch_ohlc(yahoo_symbol: str, rng: str = "10y") -> pd.DataFrame:
    """
    Yahoo v8 chart JSON → 日線 OHLCV，欄位用 core 期望的 lowercase、index 去時區。
    同一函式吃 BTC-USD / ETH-USD / AAPL / NVDA / 2330.TW，與 Binance 無關。
    台股 `.TW`（上市）查無資料時自動改試 `.TWO`（上櫃）——classify 無法預知上市/上櫃。

    ⚠️ `rng` 預設 **10y 不是 2y**：量能見頂維的分位母體就是本函式抓回來的這段歷史
    （`core.relative_high_tw.vol_pctile` 拿最新值對整段排名），而校準它的
    `scripts/tw_volwindow_calib.py` 用的是 **expanding 分位、面板自 2016-01-01 起**
    （`tw_variant_extract.py` 的 `--min-date` 預設）——live 只餵 2 年就等於拿短記憶母體
    去套長記憶母體量出來的門檻（0.95/0.85/0.70）。2026-08-11 使用者回報 6782 分位偏高，
    實查即此：同一筆近5日均量 648,800 股，2 年母體 93.5 分位、10 年母體 83.5 分位，
    量能維得分 12/18 vs 6/18。10y 起算約 2016-08，與校準面板起點對齊。
    價格類指標不受影響（MA/RSI/ATR 皆為固定窗；52 週高低由各消費端自訂窗——`watcher`
    用 `tail(252)`、`scripts/stock_profile.py` 用日曆 365 天，兩者都是固定窗故不隨 rng 變）。

    ⚠️ **不可用 `rng="max"`**：Yahoo 會自動降頻，實測 2330 的 max 只回 320 根、中位間隔
    31 天（月線），6782 回 315 根、間隔 7 天（週線）——欄名照樣是日線那套，靜默失效，
    量能分位會變成拿週/月量去比。10y 實測全市場皆為日線（2330/6509/2454/1101 各 2431 根、
    AAPL/NVDA 2512 根、BTC-USD 3653 根，中位間隔皆 1 天）。
    """
    s = _session()
    res, last_err = None, None
    for sym in _tw_candidates(yahoo_symbol):
        try:
            r = s.get(_YF_CHART + sym, params={"range": rng, "interval": "1d"}, timeout=20)
            r.raise_for_status()
            result = r.json()["chart"]["result"]
        except Exception as e:  # noqa: BLE001 — 單一候選時於迴圈外 re-raise 保留原因
            result, last_err = None, e
        if result:
            res = result[0]
            _RESOLVED[yahoo_symbol] = sym   # 記住有資料的候選，之後直打（W-3）
            break
    if res is None:
        raise RuntimeError(f"無資料：{yahoo_symbol}") from last_err
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"],
    }, index=pd.to_datetime(res["timestamp"], unit="s"))
    df = df[df["close"].notna()].copy()   # 停牌/缺資料的列剔除
    # Yahoo 對台股偶爾回「有價無量」的幽靈列（volume=0 但 OHLC 正常、當天實際有成交）：
    # 實測近 10 年 6782 有 1 筆（2026-07-10）、2454 4 筆、1101 7 筆、6509 9 筆；美股/幣對 0 筆。
    # 留著會混進量能分位母體，也會把含它的 N 日均量整段拉低。轉 NaN 而非刪列——價格那根是
    # 真的，MA/RSI/ATR 不該因此少一天。量能消費端皆為 `mean()`（預設 skipna）或 `dropna()`
    # （`relative_universal.avg_vol`/`_vol_ratio`、`relative_high_tw._vol_series`），NaN 不外溢。
    df.loc[df["volume"] <= 0, "volume"] = float("nan")
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

    ⚠️ 上櫃股（.TWO）：`classify_symbol` 一律先猜 `.TW`（上市），但 Yahoo 對上櫃股的 `.TW`
    直接 404（非暫時性、每次都一樣）。`fetch_ohlc` 已有 `_tw_candidates` 備援改試 `.TWO`，
    本函式原本沒有 → 上櫃股「現價/即時成交量」永遠失敗、每次 60s 刷新都退回日線收盤
    （2026-07-03 使用者回報：切到 6509.TW 上櫃股後現價消失，查證是 .TW 端點 404 非網路波動）。
    同樣套用 `_tw_candidates`，不多花網路成本——上市股第一個候選就成功，上櫃股才會多打一次。
    """
    for sym in _tw_candidates(yahoo_symbol):
        try:
            r = _session().get(_YF_CHART + sym,
                               params={"range": "1d", "interval": "1d"}, timeout=10)
            r.raise_for_status()
            m = r.json()["chart"]["result"][0]["meta"]
        except Exception:
            continue
        p = m.get("regularMarketPrice")
        if p is None:
            continue
        _RESOLVED[yahoo_symbol] = sym       # 記住有資料的候選，之後直打（W-3）
        return {"price": float(p), "ts": m.get("regularMarketTime"),
                "prev_close": m.get("previousClose") or m.get("chartPreviousClose"),
                "volume": m.get("regularMarketVolume")}
    return {}


def fetch_quote_meta(yahoo_symbol: str, timeout: int = 15) -> dict:
    """Yahoo v8 chart 的整份 `meta` 區（名稱／交易所／幣別等基本資料）。失敗回 {}。

    與 `fetch_live_quote` 打同一個端點但**回整份 meta**：呼叫端要的欄位（longName /
    fullExchangeName / currency…）不在 `fetch_live_quote` 的四個回傳鍵裡，過去由
    `scripts/stock_profile.py` 自己複製一份請求迴圈，並跨模組 import 本檔的私名
    `_session`／`_tw_candidates`／`_YF_CHART`——與稽核 W-7（watcher 曾直接 import
    `_vol_pctile`）同一個錯。候選清單／UA／`_RESOLVED` 語意的正本只能有一份，故升為公開名。
    名稱屬加值資訊，取不到不該中斷整份 profile → 任何失敗都吞掉換下一個候選。

    ⚠️ 刻意**不寫 `_RESOLVED`**：本函式的成功條件只是「拿得到 meta」，比
    `fetch_ohlc`／`fetch_live_quote`（要有實際資料／有 `regularMarketPrice`）寬鬆，
    用它去釘選候選會讓後續請求先試一個只有殼的 symbol。"""
    for sym in _tw_candidates(yahoo_symbol):
        try:
            r = _session().get(_YF_CHART + sym, params={"range": "1d", "interval": "1d"},
                               timeout=timeout)
            r.raise_for_status()
            return r.json()["chart"]["result"][0]["meta"] or {}
        except Exception:      # noqa: BLE001
            continue
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


def _is_us_trading_hours(now=None) -> bool:
    """粗略判斷此刻是否為美股交易時段（週一~五 09:30–16:00 美東時間）。
    不含國定假日/半日市（本模組零外部依賴），僅供顯示分流，非交易依據。
    now 供測試注入，語意同 `_is_tw_trading_hours`。"""
    import datetime
    if now is None:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return datetime.time(9, 30) <= now.time() <= datetime.time(16, 0)


def is_daily_bar_forming(last_bar_date, is_tw: bool, now=None, *, is_crypto: bool = False) -> bool:
    """
    判斷日線最後一根 K 棒是否為「今日進行式」（尚未結算收盤）。

    Yahoo v8 chart 的 1d bar 在交易時段中會即時更新今日這根（close＝當下成交價，非結算
    收盤），watcher「最新日線」若照樣顯示會跟「現價」數字重複、日期同天（2026-07-03 使用者
    回報：盤中看到「最新日線 今日 收 X」跟「現價 X」完全一樣，像多餘重複）。
    量能側更嚴重：進行式那根的 volume 只累積到當下（見 `core.relative_high_tw.vol_pctile`）。

    需同時滿足才視為「進行式」：(a) 該市場此刻正在交易時段 (b) 最後一根日期＝當地「今天」。
    只判斷 (a) 不夠：日線快取每小時才刷新一次（`UniversalMonitor.DAILY_REFRESH_SEC`），市場
    剛開盤時快取可能還停在昨天已結算的收盤，此時最後一根其實不是今天，不該被誤判為進行式
    而錯誤退回前兩天。

    is_crypto=True：幣對 24/7 無收盤，(a) 恆真——只要最後一根＝今天就一定還在累積。Yahoo
    幣對日棒以 **UTC** 為界（`fetch_ohlc` 的 index 即 UTC epoch 轉 naive），故比的是 UTC 今天。
    不特判會套到美股時段上：美股一收盤就把仍在累積的今日棒當成已結算（幣對每天有 17.5 小時
    落在此誤判區間），量能分位因此拿半天的量去比歷史整日量。

    ⚠️ `is_tw` 與 `is_crypto` **互斥**（三選一的市場別用兩個 bool 編碼，`is_crypto` 優先）；
    呼叫端一律由 `classify_symbol` 的 `kind` 衍生（`kind=="tw_stock"` / `kind=="crypto"`），
    勿手工湊出 (True, True) 這種不存在的組合。同檔 `live_quote_freshness` 亦為 bool 慣例。

    now 供測試注入（語意同 `_is_tw_trading_hours`/`_is_us_trading_hours`：代表該市場當地
    此刻的 wall-clock datetime，幣對則為 UTC，呼叫端負責建構正確時區的值）。
    """
    import datetime
    if now is None:
        if is_crypto:
            now = datetime.datetime.now(datetime.timezone.utc)
        else:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo("Asia/Taipei" if is_tw else "America/New_York"))
    # 幣對 24/7 → 條件 (a) 恆真，只剩「最後一根＝今天（UTC）」要判
    trading_now = is_crypto or (_is_tw_trading_hours(now) if is_tw else _is_us_trading_hours(now))
    return trading_now and last_bar_date == now.date()


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


def resolve_live_volume(live_volume, cached_volume, cached_ts, refresh_sec, now=None):
    """
    即時成交量若本次 fetch 缺漏則退回快取值，避免每次刷新忽有忽無地閃爍。

    根因：Yahoo `regularMarketVolume` 偶爾單次缺漏（`regularMarketPrice` 等其餘欄位正常，
    僅此欄漏），watcher 每 60s 重新 fetch，若直接以「本次有無」判斷顯示，使用者會看到這行
    忽然消失又出現（2026-07-03 使用者回報）。成交量單調累加，退回舊值不影響方向判讀。

    回傳 (display_volume, stale_note)：
      - display_volume：本次值優先，缺漏則用快取，兩者皆無則 None（呼叫端應整行不顯示）。
      - stale_note：僅在「本次確實使用快取」且快取已超過 2 個刷新週期時給簡短標註
        （如 `「（快取 130s 前）」`），提醒非本次刷新；否則為空字串（單次blip 不必打擾使用者）。
    """
    import time as _time
    if live_volume:
        return live_volume, ""
    if not cached_volume:
        return None, ""
    now = now if now is not None else _time.time()
    stale_sec = now - cached_ts
    note = f"（快取 {int(stale_sec)}s 前）" if stale_sec > 2 * refresh_sec else ""
    return cached_volume, note
