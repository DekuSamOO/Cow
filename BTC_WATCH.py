import os
import sys
import time
import math
import logging
import datetime
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
if _COW not in sys.path:
    sys.path.insert(0, _COW)
# core.risk 只依賴 math，無重依賴 → 獨立於下方 try/except 之外，極簡模式（_COW_OK=False）仍可用
from core.risk import atr_risk_rows as _atr_risk_rows

_COW_OK = False
try:
    from core.indicators import calculate_technical_indicators, calculate_ahr999
    from core.bear_bottom import calculate_bear_bottom_indicators
    from core.relative_high import compute_escape_top_score, escape_top_meta, annualize_funding
    from core.relative_low import compute_relative_low_score, relative_low_meta
    from core.trend_direction import compute_trend_score, trend_meta
    from core.momentum import momentum_ref_rows
    from core.bottom_floors import compute_all_bottom_estimates
    from core.action_ensemble import compute_composite_action
    _COW_OK = True
except Exception as _e:  # noqa: BLE001
    print(f"[警告] 無法 import Cow core（{_e}）→ 退化為極簡模式（無六維評分）。")

    def annualize_funding(rate_8h_pct):
        return None if rate_8h_pct is None else rate_8h_pct * 3 * 365


# ──────────────────────────────────────────────────────────────────────────────
# W-7（2026-07-06）：畫框/排版/面板/K線側欄/可中斷等待等終端機 UI 通用邏輯已抽至
# core/term_ui.py（watcher.py 原本直接綁死這些私名 import，任何整形都可能靜默破
# watcher；抽出後兩邊改吃同一份）。本檔以下 re-export 為既有內部呼叫端與既有測試
# （`from BTC_WATCH import _title, ...`）保留零改動的相容 shim，純位置移動不改邏輯。
# ──────────────────────────────────────────────────────────────────────────────
from core.term_ui import (         # noqa: E402,F401
    _bar, _NARROW_SYMBOLS, _dw, _row, _edge, _title, _panel, _bar_signed,
    _short_momentum, _panel_trend, _panel_stance, _cut_display,
    _SCORE_PREFIX_RE, _LIGHTS, _MIN_COL_W, _split_name_light, _row_subitems,
    _panel_name_width, _render_score_row, _wrap_display, _panel_block,
    _pair_lines, _ANSI_GREEN, _ANSI_RED, _ANSI_RESET, _enable_windows_ansi,
    _fmt_axis_price, _kline_column, _kline_panel_lines, _print_with_kline,
    interruptible_wait,
)


class BitcoinMonitor:
    """BTC 雙向監控儀表板：逃頂五維（relative_high）+ 抄底六維（relative_low）。"""

    FALLBACK_SUPPORT = 54000   # 動態地板算不出時的靜態防線（2026/5 的 0.618 值）
    DAILY_REFRESH_SEC = 3600   # 日線/地板/外部維度刷新間隔（即時項仍每 60s）
    KLINE_DAYS = 30            # 右側 K 線側欄天數（每日固定佔 1 字元寬欄位）

    # 可得天花板（純幣安 + F&G + 本地快取 ETF/SOPR/BTC.D + 本地總經事件行事曆）。
    # 唯一缺項：macro 的通膨/就業 dovish/hawkish flags（FRED 被公司網路封鎖，macro 僅拿得到
    # 事件臨近 3 分，非滿分 10）。⚠️ 不能再用「100-7」這種捷徑算：2026-07 onchain 併入
    # MVRV-Z 後單維已超編（20→26／10→16），WEIGHTS 原始總和變成 106，full-macro 情境會被
    # compute_escape_top_score/compute_relative_low_score 的 clamp(100) 蓋掉多出的 6 分，
    # 「缺 FRED」實際只再少那超編之外的 1 分，不是少 7 分——必須逐維直接加總才準：
    TOP_CAP = 99               # derivatives30 + technical25 + onchain26(含MVRV-Z) + sentiment15 + macro事件3
    LOW_CAP = 99               # cycle25 + derivatives20 + technical20 + sentiment15 + onchain16(含MVRV-Z) + macro事件3

    def __init__(self, symbol="BTCUSDT", coin_symbol="BTCUSD_PERP", is_btc=True,
                 top_cap=99, low_cap=99, title=None, oi_unit="BTC", nav=False):
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
        self._sentinel_cache = None   # (gate, d3)：升槓桿窗口／熊底 D3，隨日線刷新
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
                    # 與 dashboard/LINE 一致：供給最新算力 → final_low 含礦工電費硬地板，
                    # 杜絕主防線在三介面間漂移（取不到算力→best-effort 退回 None，行為同舊版）。
                    try:
                        from service.bottom_metrics import fetch_hashrate_history_ths
                        _hr = fetch_hashrate_history_ths()
                        _latest_hash = _hr[max(_hr)] if _hr else None
                    except Exception:
                        _latest_hash = None
                    self._floors_cache = compute_all_bottom_estimates(
                        float(df.iloc[-1]["close"]), df=df, hashrate_ths=_latest_hash, onchain=None)
                except Exception as e:
                    print(f"動態地板計算失敗：{e}")
                    self._floors_cache = None
            else:
                self._floors_cache = None
            try:
                self._compute_sentinels()
            except Exception as e:
                # 印出原因而非靜默吞掉：欄位改名／常數搬家這類程式錯誤若無聲消失，
                # 畫面只會長期少兩行、事後無從排查（沿用上方地板計算的錯誤慣例）。
                print(f"哨兵計算失敗：{e}")
                self._sentinel_cache = None
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
        通膨/就業 dovish/hawkish flags 需 FRED → 略，故 cap=99 而非 100，見 TOP_CAP/LOW_CAP 註解）。
        dashboard `_gather_radar_externals` 的精簡鏡像（同 service 單一來源）。
        """
        ext = {"etf": None, "sopr": None, "btcd": None, "macro": None,
               "mvrv_z": None}   # mvrv_z 已驗證轉正式計分（onchain 子分）
        if not self.is_btc:
            # 非 BTC 幣對：ETF(Farside BTC)/SOPR(bitcoin-data BTC)/BTC.D 皆 BTC 專屬 → 僅取本地總經事件
            try:
                from service.macro_events import get_next_macro_event
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
            _bm = get_latest_bottom_metrics()                      # bitcoin-data，12h json 持久化快取
            ext["sopr"] = _bm.get("sopr")
            ext["mvrv_z"] = _bm.get("mvrv_zscore")                 # 已驗證計入 onchain 子分（AUC 0.732）
        except Exception as e:
            print(f"SOPR/MVRV-Z 取得失敗：{e}")
        try:
            from service.market_snapshot import get_btcd_trend
            ext["btcd"] = get_btcd_trend()                   # 本地 OI 快照累積的 BTC.D 趨勢（change_pp）
        except Exception as e:
            print(f"BTC.D 趨勢取得失敗：{e}")
        try:
            from service.macro_events import get_next_macro_event
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

    def _compute_sentinels(self):
        """升槓桿窗口閘門 + 熊底 D3 的判定（單一真實來源 core.leverage_window）。

        **刻意隨日線每小時算一次、不放進 60s 的 render()**：這段只依賴日線
        （argmax／布林遮罩／argmin 都是 O(n) 全表掃描，df 常態約 1500 列），
        而 df 每 DAILY_REFRESH_SEC 才換一份——放在 render 裡等於每小時白算 59 次。
        僅 BTC 有意義（AHR999／冪律為 BTC 校準），其他幣對留 None、不佔版面。
        """
        self._sentinel_cache = None
        if not self.is_btc:
            return
        from core.leverage_window import gate_status, d3_status, find_bear_low
        from config import LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH
        df = self._daily_cache
        if df is None or len(df) < 200 or "AHR999" not in df.columns:
            return
        ahr = df["AHR999"].iloc[-1]
        if ahr is None or (isinstance(ahr, float) and math.isnan(ahr)):
            return
        hi = df["high"] if "high" in df.columns else df["close"]
        ath_pos = int(hi.values.argmax())
        closes = df["close"].values
        # 低點門檻（自 ATH 跌逾 30%）由 core.leverage_window 統一，勿在此重寫
        lo_val, lo_pos = find_bear_low(closes, float(hi.iloc[ath_pos]), ath_pos)
        if lo_val is None:
            d3 = {"ok": None}
        else:
            # 距低點天數以 K 棒位移計（此處手上就是完整日線快取）
            d3 = d3_status(float(closes[-1]), lo_val,
                           str(df.index[lo_pos])[:10], len(df) - 1 - lo_pos)
        self._sentinel_cache = (
            gate_status(float(ahr), len(df) - 1 - ath_pos,
                        LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH),
            d3,
        )

    def _sentinel_rows(self, price):
        """把快取好的哨兵狀態排成兩行橫向摘要；算不出來就整段略過、不佔版面。"""
        if not self._sentinel_cache:
            return []
        from core.leverage_window import compact_rows
        from config import LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH
        gate, d3 = self._sentinel_cache
        return compact_rows(gate, d3, price,
                            LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH)

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
        # 總持倉量沒有 $ 符號（跟現價/4h最低/動態地板/多錨中位不同），多補 1 格空白抵掉
        # 缺的那個字元寬度，讓千位符號分隔的數字跟上面 4 行的 $ 後數字對齊在同一欄。
        quote.append(f"  總持倉量       {md['total_oi']:>12,.0f} {self.oi_unit} （U {md['u_oi']:,.0f} + 幣本位 {md['coin_oi_btc']:,.0f}）")
        oi_line = "✕ 擷取失敗" if ch is None else f"{ch:+.2f}% (1h滾動)"
        pct_line = "" if pct is None else f"  |  近30日分位 {pct:.0f}%"
        quote.append(f"  OI 變化       {oi_line}{pct_line}")
        quote.append(f"  資金費率      {fr_txt}   {ann_txt}")
        # C：短線動能（補趨勢中長期軸缺的「這週」尺度，與順勢軸正交）
        if mom:
            quote.append(f"  短線動能      {mom}")
        # 時間序列動能（3/6/12M 報酬）— 參考訊號，未計入加權（待回測）
        quote += momentum_ref_rows(self._daily_cache)
        # 風控框架（ATR 停損 + 支撐壓力風報比）— 用動態地板當支撐，前高當壓力
        quote += _atr_risk_rows(self._daily_cache, md["close"], support=sup)
        # A：即時項每 60s 更新；日線/地板/外部維度每小時刷新一次
        quote.append(f"  數據時效      即時 60s｜日線·地板·外部 {data_age}")
        # 升槓桿窗口／熊底確認哨兵（2026-08-25）——刻意做成兩行橫向摘要塞進本區，
        # 不另開面板：兩欄面板已把 W 撐到 >=102，而本區各行僅約 50-58 寬，
        # 右側有約 44 字元的閒置橫向空間，放這裡不會撐寬版面、不增加垂直高度。
        quote += self._sentinel_rows(md["close"])

        _, top_rows = _panel(top, escape_top_meta, self.TOP_CAP, "逃頂訊號（出貨）",
                             ("derivatives", "technical", "onchain", "sentiment", "macro"))
        _, low_rows = _panel(low, relative_low_meta, self.LOW_CAP, "抄底訊號（進場）",
                             ("cycle", "derivatives", "technical", "sentiment", "onchain", "macro"))
        _, trend_rows = _panel_trend(trend, "趨勢方向（順勢）",
                                     ("ma_structure", "macd", "slope", "adx"))

        # 三軸融合操作訊號（頭條）：逃頂(貴)＋抄底(便宜)＋趨勢(方向) → 一個 stance。
        # 單看任一軸會漏判（2026-05 $82k→$59k：逃頂全程低、真正示警的是趨勢軸）。需三軸皆有才算。
        ct_comp, comp_rows = "", []
        if top is not None and low is not None and trend is not None:
            act = compute_composite_action(
                trend[0], top[0], low[0], low[1]["cycle"]["score"])
            if act:
                _, comp_rows = _panel_stance(
                    "操作", f"{act['emoji']} {act['action']}", act["detail"])
                comp_rows.append(f"     {act['pos_label']}")
                ct_comp = f"操作  {act['emoji']} {act['action']}"

        # 兩欄並排：操作｜趨勢、逃頂｜抄底（省垂直高度、一頁看完）。緊湊標題（略「訊號/可得」字樣）。
        ct_trend = (f"趨勢  {trend[0]:+d}/±100  {_bar_signed(trend[0])}  {trend_meta(trend[0])[0]}"
                    if trend is not None else "")
        ct_top = (f"逃頂  {top[0]}/100  ≤{self.TOP_CAP}  {_bar(top[0], self.TOP_CAP)}  {escape_top_meta(top[0])[0]}"
                  if top is not None else "")
        ct_low = (f"抄底  {low[0]}/100  ≤{self.LOW_CAP}  {_bar(low[0], self.LOW_CAP)}  {relative_low_meta(low[0])[0]}"
                  if low is not None else "")
        panels = [(t, r) for t, r in ((ct_comp, comp_rows), (ct_trend, trend_rows),
                                      (ct_top, top_rows), (ct_low, low_rows)) if r]

        # 全寬區（表頭 / 即時行情）自然寬度；兩欄區各欄需容得下緊湊標題，故 W 至少 2 欄寬。
        w_full = max((_dw(c) for c in (header + quote)), default=40)
        # 每欄至少 _MIN_COL_W 寬：容得下標題、及對齊後最長子項（如 Mayer「…×0.8 (極度低估)」約 46）
        col_need = max((_dw(t) for t, _ in panels), default=40) + 4   # +4：┌─ … ─┐ 邊距
        col_need = max(col_need, _MIN_COL_W)
        W = max(w_full, 2 * col_need + 2, _dw("即時行情") + 4)
        wl = (W - 2) // 2
        wr = (W - 2) - wl

        left = [
            _edge("╔", "═", "╗", W),
            _row(header[0], W, "║"),
            _edge("╠", "═", "╣", W),
            _row(header[1], W, "║"),
            _row(header[2], W, "║"),
            _edge("╚", "═", "╝", W),
            "",
            _title("即時行情", W),
        ]
        left += [_row(r, W) for r in quote]
        left.append(_edge("└", "─", "┘", W))

        for i in range(0, len(panels), 2):
            left.append("")
            if i + 1 < len(panels):
                left.extend(_pair_lines(panels[i], panels[i + 1], wl, wr))
            else:                                    # 奇數面板 → 最後一個佔全寬（同用 _panel_block 對齊）
                left.extend(_panel_block(panels[i][0], panels[i][1], W))

        hint = "b 重選代號｜q 結束" if self.nav else "Ctrl+C 結束"
        left.append("")
        left.append(f"  下次刷新 {nxt}    （{hint}）")

        _print_with_kline(left, W, self._daily_cache, self.KLINE_DAYS, enabled=_COW_OK)

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
                        btc_d_trend=ext.get("btcd"), macro=ext.get("macro"), mvrv_z=ext.get("mvrv_z"))
                    low = compute_relative_low_score(
                        row, df, funding_8h=funding_8h, oi_stats=oi_stats, fng=self._fng_cache,
                        etf_summary=ext.get("etf"), sopr=ext.get("sopr"),
                        btc_d_trend=ext.get("btcd"), macro=ext.get("macro"), mvrv_z=ext.get("mvrv_z"))
                    trend = compute_trend_score(row, df)
                    mom = _short_momentum(df)
                self.render(md, funding, oi_stats, top, low, trend, mom)
            else:
                self.render_simple(md, funding, oi_stats)

            cmd = interruptible_wait(60, nav=self.nav)
            if cmd == "exec":
                continue            # e 鍵日誌行為僅 UniversalMonitor 有（E3）；BTC 版忽略
            if cmd:
                return cmd          # 'back'（回上層重選）/ 'quit'（結束）→ 交給 watcher 處理


if __name__ == "__main__":
    BitcoinMonitor().run()
