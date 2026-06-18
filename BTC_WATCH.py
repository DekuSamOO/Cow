import os
import sys
import time
import math
import logging
import datetime
import unicodedata
import requests
import urllib3

# 依據公司本地端網路環境需求，完整關閉 SSL 驗證並忽略警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# service 層部分函式掛 @st.cache_data，無 Streamlit runtime 時會降級記憶體快取並印警告（功能正常）。
# BTC_WATCH 每秒清屏顯示，抑制這些警告避免洗版。
logging.getLogger("streamlit").setLevel(logging.ERROR)

# ──────────────────────────────────────────────────────────────────────────────
# 單一真實來源：import 同 repo 的相對高點/相對底部評分（正本即在 Cow 根目錄）
#   逃頂五維（relative_high）+ 抄底六維（relative_low）+ 動態地板（bottom_floors）
#   的閾值與評分邏輯只存在 Cow core 一份，Cow 一改這裡立即吃到，杜絕兩邊漂移。
#   路徑取自本檔案所在目錄（換機/搬資料夾不需改碼）；
#   ImportError fallback：環境缺套件時退化為極簡模式（僅價格/資金費率/OI，無評分）。
# ──────────────────────────────────────────────────────────────────────────────
_COW = os.path.dirname(os.path.abspath(__file__))
_COW_OK = False
try:
    if _COW not in sys.path:
        sys.path.insert(0, _COW)
    from core.indicators import calculate_technical_indicators, calculate_ahr999
    from core.bear_bottom import calculate_bear_bottom_indicators
    from core.relative_high import (compute_escape_top_score, escape_top_meta,
                                    annualize_funding)
    from core.relative_low import compute_relative_low_score, relative_low_meta
    from core.trend_direction import compute_trend_score, trend_meta
    from core.bottom_floors import compute_all_bottom_estimates
    from core.composite_signal import compute_composite_signal
    _COW_OK = True
except Exception as _e:  # noqa: BLE001
    print(f"[警告] 無法 import Cow core（{_e}）→ 退化為極簡模式（無六維評分）。")

    def annualize_funding(rate_8h_pct):
        return None if rate_8h_pct is None else rate_8h_pct * 3 * 365


# 評分等級→燈號（逃頂與底部共用底色概念）
def _bar(score, cap):
    """以可得天花板 cap 為分母畫 10 格進度條。"""
    cap = max(cap, 1)
    filled = int(round(min(score, cap) / cap * 10))
    return "█" * filled + "░" * (10 - filled)


def _dw(s):
    """字串顯示寬度：全形/emoji=2、半形=1（FE0F 修飾符=0）。"""
    w = 0
    for ch in s:
        if ch == "️":
            continue
        o = ord(ch)
        if 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF:
            w += 2
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _row(content, W, v="│"):
    """左右框 + 內容補空格對齊到顯示寬度 W。v 為側框字元（雙線表頭用 ║）。"""
    return v + content + " " * max(0, W - _dw(content)) + v


def _edge(left, fill, right, W):
    return left + fill * W + right


def _title(text, W):
    """┌─ text ───┐ 形式，依顯示寬度補滿右框。"""
    head = f"─ {text} "
    return "┌" + head + "─" * max(0, W - _dw(head)) + "┐"


def _panel(result, meta_fn, cap, name, dims):
    """逃頂/抄底評分共用：把 (score, signals) 攤成 (title, rows)。result 為 None 時回 ("", [])。"""
    if result is None:
        return "", []
    score, sig = result
    level, _, action = meta_fn(score)
    title = f"{name}  {score}/100  可得≤{cap}  {_bar(score, cap)}  {level}"
    rows = [f"  {sig[d]['score']:>2}/{sig[d]['max']:<2}  {sig[d]['label']}" for d in dims]
    rows.append(f"  → {action}")
    return title, rows


def _bar_signed(net):
    """有號淨方向分（-100~+100）置中條：│ 左為空頭、右為多頭，各 5 格。"""
    mag = int(round(min(abs(net), 100) / 100 * 5))
    if net >= 0:
        return "░" * 5 + "│" + "█" * mag + "░" * (5 - mag)
    return "░" * (5 - mag) + "█" * mag + "│" + "░" * 5


def _short_momentum(df):
    """
    短線動能（補趨勢方向中長期軸缺的「這週」尺度）：近 7 日報酬 + 價 vs EMA_20 + RSI_14。
    純取已算好的日線欄位（每小時刷新一次的 df），不發網路請求；資料不足回 None。
    與趨勢方向軸正交：可「中期空頭 + 短線偏多」（短線反彈），正是區分「全面下跌 vs 反彈」之用。
    """
    if df is None or len(df) < 8:
        return None
    close = float(df["close"].iloc[-1])
    prev7 = float(df["close"].iloc[-8])
    ret7 = (close / prev7 - 1) * 100 if prev7 else 0.0

    def _last(col):
        if col not in getattr(df, "columns", []):
            return None
        v = df[col].iloc[-1]
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    ema20 = _last("EMA_20")
    rsi = _last("RSI_14")
    above = None if ema20 is None else close > ema20

    # 近 7 日方向與價對 EMA20 一致才表態（above is False 僅在 ema20 存在時成立）
    if ret7 > 0 and above is True:
        lbl = "🟢 短線偏多"
    elif ret7 < 0 and above is False:
        lbl = "🔴 短線偏空"
    else:
        lbl = "⚪ 短線中性"

    parts = [f"近7日 {ret7:+.1f}%"]
    if above is not None:
        parts.append("價>EMA20" if above else "價<EMA20")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    return f"{lbl}  " + "｜".join(parts)


def _panel_trend(result, name, dims):
    """趨勢方向專用：分數有號（多+/空−），不適用 0-100 進度條與「可得≤」語意。"""
    if result is None:
        return "", []
    net, sig = result
    level, _, action = trend_meta(net)
    title = f"{name}  {net:+d}/±100  {_bar_signed(net)}  {level}"
    rows = [f"  {sig[d]['score']:+3d}/±{sig[d]['max']:<2} {sig[d]['label']}" for d in dims]
    rows.append(f"  → {action}")
    return title, rows


def interruptible_wait(seconds, nav=False):
    """
    等待 seconds 秒。nav=True（由 watcher 進入）時偵測鍵盤指令並提早返回：
      b / Enter → 'back'（回上層重選代號）；q → 'quit'（結束）。回傳指令字串或 None。
    nav=False（BTC_WATCH 單獨執行）或非 Windows 無 msvcrt → 純 sleep、不收指令（行為不變）。
    """
    if not nav:
        time.sleep(seconds)
        return None
    try:
        import msvcrt
    except ImportError:
        time.sleep(seconds)
        return None
    end = time.time() + seconds
    while time.time() < end:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"b", b"B", b"\r", b"\n"):
                return "back"
            if ch in (b"q", b"Q"):
                return "quit"
        time.sleep(0.1)
    return None


class BitcoinMonitor:
    """BTC 雙向監控儀表板：逃頂五維（relative_high）+ 抄底六維（relative_low）。"""

    FALLBACK_SUPPORT = 54000   # 動態地板算不出時的靜態防線（2026/5 的 0.618 值）
    DAILY_REFRESH_SEC = 3600   # 日線/地板/外部維度刷新間隔（即時項仍每 60s）

    # 可得天花板（純幣安 + F&G + 本地快取 ETF/SOPR/BTC.D + 本地總經事件行事曆）。
    # 唯一缺項：macro 的通膨/就業 dovish/hawkish flags（FRED 被公司網路封鎖）= 7 分 → 100-7=93。
    TOP_CAP = 93               # derivatives30 + technical25 + onchain20 + sentiment15 + macro事件3
    LOW_CAP = 93               # cycle25 + derivatives20 + technical20 + sentiment15 + onchain10 + macro事件3

    def __init__(self, symbol="BTCUSDT", coin_symbol="BTCUSD_PERP", is_btc=True,
                 top_cap=93, low_cap=93, title=None, oi_unit="BTC", nav=False):
        self.fapi_url = "https://fapi.binance.com/fapi/v1"
        self.fdata_url = "https://fapi.binance.com/futures/data"
        self.symbol = symbol
        self.coin_symbol = coin_symbol   # 幣本位永續 symbol（None=該標的無幣本位合約 → 略過）
        self.is_btc = is_btc             # False=非 BTC 幣對：停用 ETF/SOPR/BTC.D/四季論/礦工/冪律
        self.TOP_CAP = top_cap           # 實例級可得天花板（覆蓋下方 class 預設）
        self.LOW_CAP = low_cap
        self.title = title or "BTC 雙向監控儀表板 · 逃頂五維 + 抄底六維"
        self.oi_unit = oi_unit           # 總持倉量單位標籤（BTC / ETH / SOL…）
        self.nav = nav                   # True=由 watcher 進入 → 儀表板內可按鍵回上層/結束

        # 日線 DataFrame + 動態地板每小時刷新一次（避免 60s 迴圈重抓重算）
        self._daily_cache = None
        self._floors_cache = None
        self._fng_cache = None
        self._ext_cache = None     # ETF/SOPR/BTC.D/macro 外部維度（每小時隨日線刷新一次）
        self._daily_ts = 0.0

    # ── 即時資料 ──────────────────────────────────────────────────────────────
    def _session(self):
        s = requests.Session()
        s.verify = False
        return s

    def get_market_data(self):
        """即時價格 + 4h 最低 + U本位與幣本位加總總持倉量（顯示用）。"""
        try:
            s = self._session()
            kl = s.get(f"{self.fapi_url}/klines",
                       params={"symbol": self.symbol, "interval": "4h", "limit": 2},
                       timeout=10).json()
            low_price = float(kl[-1][3])
            close_price = float(kl[-1][4])

            u_oi = float(s.get(f"{self.fapi_url}/openInterest",
                               params={"symbol": self.symbol}, timeout=10).json()
                         .get("openInterest", 0))
            # 幣本位永續 OI（僅部分標的有；coin_symbol=None 或抓取失敗 → 計 0 略過）
            coin_oi_btc = 0.0
            if self.coin_symbol:
                try:
                    coin_contracts = float(s.get("https://dapi.binance.com/dapi/v1/openInterest",
                                                 params={"symbol": self.coin_symbol}, timeout=10)
                                           .json().get("openInterest", 0))
                    coin_oi_btc = (coin_contracts * 100) / close_price if close_price else 0.0
                except Exception:
                    coin_oi_btc = 0.0
            return {"close": close_price, "low4h": low_price,
                    "u_oi": u_oi, "coin_oi_btc": coin_oi_btc,
                    "total_oi": u_oi + coin_oi_btc}
        except Exception as e:
            print(f"數據擷取失敗：{e}")
            return None

    def get_funding_rate(self):
        """即時資金費率（% / 8h）。premiumIndex.lastFundingRate 為小數，×100 轉百分比。"""
        try:
            r = self._session().get(f"{self.fapi_url}/premiumIndex",
                                    params={"symbol": self.symbol}, timeout=10).json()
            return float(r["lastFundingRate"]) * 100
        except Exception as e:
            print(f"資金費率擷取失敗：{e}")
            return None

    def get_oi_stats(self):
        """
        OI 統計（取代舊「相鄰兩輪 60s 差值」失效邏輯）：
          - 5m×13 ≈ 1 小時滾動 ΔOI%（清洗/堆積；底部與逃頂共用）
          - 1d×30 → 當前 OI 在近 30 日的百分位（逃頂過載；底部 _score 不用）
        回傳 {change_1h_pct, percentile, is_near_high}；失敗的項為 None。
        """
        stats = {"change_1h_pct": None, "percentile": None, "is_near_high": None}
        s = self._session()
        try:
            short = s.get(f"{self.fdata_url}/openInterestHist",
                          params={"symbol": self.symbol, "period": "5m", "limit": 13},
                          timeout=10).json()
            vals = [float(d["sumOpenInterest"]) for d in short]
            if len(vals) >= 2 and vals[0] > 0:
                stats["change_1h_pct"] = (vals[-1] / vals[0] - 1) * 100
        except Exception as e:
            print(f"OI 短期統計失敗：{e}")
        try:
            daily = s.get(f"{self.fdata_url}/openInterestHist",
                          params={"symbol": self.symbol, "period": "1d", "limit": 30},
                          timeout=10).json()
            dv = [float(d["sumOpenInterest"]) for d in daily]
            if len(dv) >= 5:
                cur = dv[-1]
                stats["percentile"] = sum(1 for v in dv if v <= cur) / len(dv) * 100
                stats["is_near_high"] = cur >= max(dv) * 0.99
        except Exception as e:
            print(f"OI 分位統計失敗：{e}")
        return stats

    def get_fng(self):
        """恐懼貪婪指數（alternative.me，免費無 key，日更）。回傳 0-100 或 None。"""
        try:
            r = self._session().get("https://api.alternative.me/fng/",
                                    params={"limit": 1, "format": "json"}, timeout=10).json()
            return float(r["data"][0]["value"])
        except Exception as e:
            print(f"F&G 擷取失敗：{e}")
            return None

    def _refresh_daily(self):
        """近 1000 日日線 → 指標 + 熊底指標 + 動態地板，每小時刷新一次。"""
        if not _COW_OK:
            return
        now = time.time()
        if self._daily_cache is not None and (now - self._daily_ts) < self.DAILY_REFRESH_SEC:
            return
        try:
            import pandas as pd
            r = self._session().get(
                f"{self.fapi_url}/klines",
                params={"symbol": self.symbol, "interval": "1d", "limit": 1500}, timeout=20)
            data = r.json()
            rows = [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "volume": float(k[5])} for k in data]
            idx = pd.to_datetime([k[0] for k in data], unit="ms")
            df = pd.DataFrame(rows, index=idx)
            df = calculate_technical_indicators(df)
            df = calculate_ahr999(df)
            df = calculate_bear_bottom_indicators(df)
            if not self.is_btc:
                # 冪律/AHR999 為 BTC 校準 → 其他幣對 NaN 化，使 cycle 冪律子維優雅停用（給 0 分）
                for col in ("PowerLaw_Ratio", "PowerLaw_Support", "AHR999"):
                    if col in df.columns:
                        df[col] = float("nan")
            self._daily_cache = df
            # 動態地板（四季論/礦工/冪律皆 BTC 專屬）→ 僅 BTC 計算；其他幣對於 _support_line 改用 Mayer 估值底
            if self.is_btc:
                try:
                    self._floors_cache = compute_all_bottom_estimates(
                        float(df.iloc[-1]["close"]), df=df, hashrate_ths=None, onchain=None)
                except Exception as e:
                    print(f"動態地板計算失敗：{e}")
                    self._floors_cache = None
            else:
                self._floors_cache = None
            self._fng_cache = self.get_fng()
            self._ext_cache = self._gather_externals()
            self._daily_ts = now
        except Exception as e:
            print(f"日線刷新失敗：{e}")

    def _gather_externals(self):
        """
        ETF / SOPR / BTC.D / macro 外部維度（隨日線每小時刷新一次，避免 60s 迴圈與 bitcoin-data 限流）。
        全 best-effort：抓不到的維度回 None → 評分自動灰燈、不 crash。皆為本地快取或輕量單點，
        **不打被公司網路封鎖的 FRED**（macro 僅取本地事件行事曆 event_within_days；
        通膨/就業 dovish/hawkish flags 需 FRED → 略，故 cap=93 而非 100）。
        dashboard `_gather_radar_externals` 的精簡鏡像（同 service 單一來源）。
        """
        ext = {"etf": None, "sopr": None, "btcd": None, "macro": None}
        if not self.is_btc:
            # 非 BTC 幣對：ETF(Farside BTC)/SOPR(bitcoin-data BTC)/BTC.D 皆 BTC 專屬 → 僅取本地總經事件
            try:
                from service.macro_data import get_next_macro_event
                days = get_next_macro_event().get("days")
                ext["macro"] = {"event_within_days": days} if days is not None else None
            except Exception as e:
                print(f"總經事件取得失敗：{e}")
            return ext
        try:
            from service.etf_flow import get_etf_flow_summary
            ext["etf"] = get_etf_flow_summary()              # committed db/etf_flow.json（Farside 403 備援）
        except Exception as e:
            print(f"ETF 摘要取得失敗：{e}")
        try:
            from service.bottom_metrics import get_latest_bottom_metrics
            ext["sopr"] = get_latest_bottom_metrics().get("sopr")   # bitcoin-data，12h json 持久化快取
        except Exception as e:
            print(f"SOPR 取得失敗：{e}")
        try:
            from service.market_snapshot import get_btcd_trend
            ext["btcd"] = get_btcd_trend()                   # 本地 OI 快照累積的 BTC.D 趨勢（change_pp）
        except Exception as e:
            print(f"BTC.D 趨勢取得失敗：{e}")
        try:
            from service.macro_data import get_next_macro_event
            days = get_next_macro_event().get("days")        # db/macro_events.json（本地，不碰 FRED）
            ext["macro"] = {"event_within_days": days} if days is not None else None
        except Exception as e:
            print(f"總經事件取得失敗：{e}")
        return ext

    def _support_line(self):
        """主防線：動態 final_low，取不到時退回靜態 54000。回傳 (value, basis, ensemble)。"""
        f = self._floors_cache
        if f and f.get("final_low"):
            return f["final_low"], f.get("final_low_basis", "動態地板"), f.get("ensemble_low")
        if not self.is_btc:
            # 非 BTC 幣對無 BTC 動態地板 → 改用 Mayer 估值底（2年均×0.6）；算不出則不顯示
            df = self._daily_cache
            if df is not None and not df.empty:
                sma730 = df.iloc[-1].get("SMA_730")
                if sma730 is not None and not (isinstance(sma730, float) and math.isnan(sma730)) and sma730 > 0:
                    return float(sma730) * 0.6, "Mayer 估值底(2年均×0.6)", None
            return None, None, None
        return self.FALLBACK_SUPPORT, "fallback 靜態防線", None

    # ── 畫面 ────────────────────────────────────────────────────────────────────
    def render(self, md, funding, oi_stats, top, low, trend=None, mom=None):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nxt = (datetime.datetime.now() + datetime.timedelta(seconds=60)).strftime("%H:%M:%S")
        src = "Cow 單一來源" if _COW_OK else "fallback 極簡模式"

        sup, basis, ens = self._support_line()
        ch = oi_stats.get("change_1h_pct")
        pct = oi_stats.get("percentile")
        # D：擷取失敗（except → None）與真正中性值區分，失敗用 ✕ 不混為市場訊號
        if funding is None:
            fr_txt, ann_txt = "✕ 擷取失敗", ""
        else:
            fr_txt = f"{funding:.4f}%/8h"
            ann = annualize_funding(funding)
            ann_txt = "—" if ann is None else f"年化 {ann:.0f}%"

        # A：日線/地板/外部維度最後刷新時間（每小時一次）+ 漏刷警示
        if self._daily_ts:
            stale_sec = 2 * self.DAILY_REFRESH_SEC   # 漏刷一次（逾兩個刷新週期）才警示
            stamp = datetime.datetime.fromtimestamp(self._daily_ts).strftime("%H:%M:%S")
            warn = f"  ⚠ 已逾{stale_sec // 3600}h未更新" if time.time() - self._daily_ts > stale_sec else ""
            data_age = f"{stamp} 刷新{warn}"
        else:
            data_age = "尚未刷新"

        header = [
            f"  {self.title}",
            f"  監測時間  {now}     邏輯來源  {src}",
            f"  交易對    {self.symbol}     刷新週期  60 秒",
        ]
        quote = [
            f"  現價          ${md['close']:>12,.0f}",
            f"  4h 最低       ${md['low4h']:>12,.0f}",
        ]
        # 地板僅在算得出時顯示（非 BTC 幣對可能無估值底）
        if sup is not None:
            drop_to = (sup / md["close"] - 1) * 100 if md["close"] else 0.0
            quote.append(f"  動態地板      ${sup:>12,.0f}   （{basis}，需 {drop_to:+.1f}% 觸及）")
        if ens:
            quote.append(f"  多錨中位      ${ens:>12,.0f}   （需 {(ens/md['close']-1)*100:+.1f}% 觸及）")
        quote.append(f"  總持倉量      {md['total_oi']:>12,.0f} {self.oi_unit} （U {md['u_oi']:,.0f} + 幣本位 {md['coin_oi_btc']:,.0f}）")
        oi_line = "✕ 擷取失敗" if ch is None else f"{ch:+.2f}% (1h滾動)"
        pct_line = "" if pct is None else f"  |  近30日分位 {pct:.0f}%"
        quote.append(f"  OI 變化       {oi_line}{pct_line}")
        quote.append(f"  資金費率      {fr_txt}   {ann_txt}")
        # C：短線動能（補趨勢中長期軸缺的「這週」尺度，與順勢軸正交）
        if mom:
            quote.append(f"  短線動能      {mom}")
        # A：即時項每 60s 更新；日線/地板/外部維度每小時刷新一次
        quote.append(f"  數據時效      即時 60s｜日線·地板·外部 {data_age}")

        top_title, top_rows = _panel(top, escape_top_meta, self.TOP_CAP, "逃頂訊號（出貨）",
                                     ("derivatives", "technical", "onchain", "sentiment", "macro"))
        low_title, low_rows = _panel(low, relative_low_meta, self.LOW_CAP, "抄底訊號（進場）",
                                     ("cycle", "derivatives", "technical", "sentiment", "onchain", "macro"))
        trend_title, trend_rows = _panel_trend(trend, "趨勢方向（順勢）",
                                               ("ma_structure", "macd", "slope", "adx"))

        # 三軸融合操作訊號（頭條）：逃頂(貴)＋抄底(便宜)＋趨勢(方向) → 一個 stance。
        # 單看任一軸會漏判（2026-05 $82k→$59k：逃頂全程低、真正示警的是趨勢軸）。需三軸皆有才算。
        comp_title, comp_rows = "", []
        if top is not None and low is not None and trend is not None:
            _, c_lvl, _, c_act = compute_composite_signal(
                trend[0], top[0], low[0], low[1]["cycle"]["score"])
            comp_title = f"操作訊號（三軸融合）  {c_lvl}"
            comp_rows = [f"  → {c_act}"]

        # 動態框寬 = 最長內容/標題行 + 邊距（右框一律對齊，cycle 長行不溢出）
        content_w = max((_dw(c) for c in (header + quote + top_rows + low_rows + trend_rows + comp_rows)), default=40)
        title_w = max(_dw(t) for t in (top_title, low_title, trend_title, comp_title or "操作訊號", "即時行情")) + 4
        W = max(content_w, title_w) + 2

        print(_edge("╔", "═", "╗", W))
        print(_row(header[0], W, "║"))
        print(_edge("╠", "═", "╣", W))
        print(_row(header[1], W, "║"))
        print(_row(header[2], W, "║"))
        print(_edge("╚", "═", "╝", W))

        print()
        print(_title("即時行情", W))
        for r in quote:
            print(_row(r, W))
        print(_edge("└", "─", "┘", W))

        if comp_rows:
            print()
            print(_title(comp_title, W))
            for r in comp_rows:
                print(_row(r, W))
            print(_edge("└", "─", "┘", W))

        if trend is not None:
            print()
            print(_title(trend_title, W))
            for r in trend_rows:
                print(_row(r, W))
            print(_edge("└", "─", "┘", W))

        if top is not None:
            print()
            print(_title(top_title, W))
            for r in top_rows:
                print(_row(r, W))
            print(_edge("└", "─", "┘", W))

        if low is not None:
            print()
            print(_title(low_title, W))
            for r in low_rows:
                print(_row(r, W))
            print(_edge("└", "─", "┘", W))

        hint = "b 重選代號｜q 結束" if self.nav else "Ctrl+C 結束"
        print(f"\n  下次刷新 {nxt}    （{hint}）")

    def render_simple(self, md, funding, oi_stats):
        """Cow 不可用時的極簡畫面。"""
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ch = oi_stats.get("change_1h_pct")
        ann = annualize_funding(funding) if funding is not None else None
        print(f"[極簡模式] {now}   {self.symbol}")
        print(f"  現價 ${md['close']:,.0f} | 4h低 ${md['low4h']:,.0f} | 總OI {md['total_oi']:,.0f} BTC")
        print(f"  OI 1h變化 {'—' if ch is None else f'{ch:+.2f}%'} | "
              f"資金費率 {'—' if funding is None else f'{funding:.4f}%/8h'} "
              f"{'' if ann is None else f'(年化 {ann:.0f}%)'}")
        print("  （Cow core 不可用，無六維評分）")

    # ── 主迴圈 ────────────────────────────────────────────────────────────────
    def run(self):
        while True:
            md = self.get_market_data()
            if md is None:
                print("數據擷取失敗，10 秒後重試…")
                time.sleep(10)
                continue
            funding = self.get_funding_rate()
            oi_stats = self.get_oi_stats()

            top = low = trend = mom = None
            if _COW_OK:
                self._refresh_daily()
                df = self._daily_cache
                if df is not None and not df.empty:
                    row = df.iloc[-1]
                    funding_8h = funding   # 已是 %（×100 過）
                    ext = self._ext_cache or {}
                    top = compute_escape_top_score(
                        row, df, funding_8h=funding_8h, oi_stats=oi_stats, fng=self._fng_cache,
                        etf_summary=ext.get("etf"), sopr=ext.get("sopr"),
                        btc_d_trend=ext.get("btcd"), macro=ext.get("macro"))
                    low = compute_relative_low_score(
                        row, df, funding_8h=funding_8h, oi_stats=oi_stats, fng=self._fng_cache,
                        etf_summary=ext.get("etf"), sopr=ext.get("sopr"),
                        btc_d_trend=ext.get("btcd"), macro=ext.get("macro"))
                    trend = compute_trend_score(row, df)
                    mom = _short_momentum(df)
                self.render(md, funding, oi_stats, top, low, trend, mom)
            else:
                self.render_simple(md, funding, oi_stats)

            cmd = interruptible_wait(60, nav=self.nav)
            if cmd:
                return cmd          # 'back'（回上層重選）/ 'quit'（結束）→ 交給 watcher 處理


if __name__ == "__main__":
    BitcoinMonitor().run()
