"""
watcher.py · 通用標的監控入口

`python watcher.py` 進入後先輸入代號（BTCUSDT / 2330 / QQQ …），自動判定市場類別後
進入對應深度的監控儀表板：

  • BTC（BTCUSDT/BTC/…）   → 完整 BitcoinMonitor 雙向儀表板（逃頂五維＋抄底六維，原封不動）
  • 其他幣對 / 美股 / 台股 → UniversalMonitor 通用儀表板（趨勢方向±100＋技術＋短線動能）

為何非 BTC 標的看不到逃頂/抄底：那兩條量表近半維度吃加密永續（資金費率/OI）與 BTC 鏈上/
減半週期（礦工成本/四季論/冪律），股票無對應資料、其他幣對也僅部分適用。本版「先做通用軸」，
股票版逃頂/抄底（融資融券/法人/期權 IV）列為後續 Phase。

通用軸資料源走 Yahoo v8 chart（service/ohlc_universal），與 Binance 無關，過公司 SSL。
評分邏輯重用 Cow core（trend_direction / indicators），與 BTC_WATCH 同一份單一真實來源。
"""
import os
import sys
import time
import datetime

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_COW = os.path.dirname(os.path.abspath(__file__))
if _COW not in sys.path:
    sys.path.insert(0, _COW)

from core.indicators import calculate_technical_indicators          # noqa: E402
from core.trend_direction import compute_trend_score, trend_meta    # noqa: E402
from core.action_ensemble import compute_trend_stance, compute_composite_action  # noqa: E402
from core.relative_high_tw import (compute_relative_high_tw, relative_high_tw_meta,  # noqa: E402
                                   vol_snapshot, VOL_WINDOW)
from core.relative_low_tw import compute_relative_low_tw, relative_low_tw_meta     # noqa: E402
from service.ohlc_universal import (classify_symbol, fetch_ohlc,            # noqa: E402
                                    fetch_live_quote, live_quote_freshness, KIND_LABEL,
                                    is_daily_bar_forming, resolve_live_volume)
# W-7（2026-07-06）：畫框/面板/等待 helper 改吃 core/term_ui（單一真實來源），
# 不再綁死 BTC_WATCH 內部私名——BTC_WATCH 整形不會再靜默破 watcher。
from core.momentum import momentum_ref_rows                             # noqa: E402
from core.watch_plan import get_plan, load_plans_cached, plan_panel_rows  # noqa: E402
from core.watch_alerts import (check_price_events, check_signal_change,   # noqa: E402
                               banner_rows, notify_beep, journal_append, journal_record)
from core.risk import atr_risk_rows as _atr_risk_rows                    # noqa: E402
from core.term_ui import (_title, _row, _edge, _dw, _bar, _bar_signed,   # noqa: E402
                          _panel, _short_momentum, _panel_trend, _panel_stance,
                          interruptible_wait, _pair_lines, _wrap_display,
                          _panel_block, _MIN_COL_W, _print_with_kline, kline_mas_for)
from BTC_WATCH import BitcoinMonitor                                     # noqa: E402


def _fmt_price(v: float) -> str:
    """跨標的價格格式：大數兩位、個位數多給小數（小市值幣）。"""
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


def _composite_panel(trend0: int, high0: int, low0: int):
    """三軸 composite 操作面板（趨勢方向＋逃頂＋抄底）→ (ct_comp, comp_rows, act)。
    台股／美股分支邏輯完全相同（皆不傳 cycle_score，見呼叫端註解），抽共用避免複製貼上。
    act 一併回傳供警戒引擎偵測 action_key 變化（E2）。"""
    act = compute_composite_action(trend0, high0, low0)
    _, comp_rows = _panel_stance("操作", f"{act['emoji']} {act['action']}", act["detail"])
    comp_rows.append(f"     {act['pos_label']}")
    ct_comp = f"操作  {act['emoji']} {act['action']}"
    return ct_comp, comp_rows, act


class UniversalMonitor:
    """非 BTC 標的的通用監控（趨勢方向＋技術＋短線動能）。資料走 Yahoo v8 chart。"""

    REFRESH_SEC = 60            # 畫面刷新（時鐘）
    DAILY_REFRESH_SEC = 3600    # 日線重抓+重算間隔（日 K 不會分鐘級變動，比照 BitcoinMonitor）
    RETRY_REFRESH_SEC = 300     # 日線刷新失敗（有快取沿用時）下次重試間隔
    MAX_TRANSIENT_FAILS = 3     # 連續暫時性失敗達此次數 → 自動回代號選單（不無限重試）
    BG_QUOTE_SEC = 300          # 背景標的（watch_plan 其餘代號）觸價巡檢間隔
    BG_QUOTE_MAX = 8            # 每輪巡檢的背景標的上限
    # 為何背景要獨立節流：原本每 60s 主迴圈都把背景標的全掃一遍 → 本標的現價 1 發 +
    # 背景 8 發 = 9 req/min ≈ 540 req/hr，這 repo 有 Yahoo 429 限流前例（見
    # service/ohlc_universal 檔頭）。背景是「盯一檔不漏他檔」的兜底，不需要 60 秒粒度；
    # 改 300s 後降到約 2.6 req/min。代價是背景觸價最多晚 5 分鐘通知（本標的仍是 60s）。

    def __init__(self, info: dict):
        self.info = info            # {kind, display, yahoo, is_btc}
        self.display = info["display"]
        self.yahoo = info["yahoo"]
        self.kind_label = KIND_LABEL.get(info["kind"], info["kind"])
        self.is_tw = info["kind"] == "tw_stock"   # 台股有逃頂/抄底籌碼面板；美股僅通用軸
        self.is_crypto = info["kind"] == "crypto"  # 幣對 24/7：今日日棒判定不能套美股收盤時段
        self._daily_cache = None
        self._daily_ts = 0.0
        self._chip = None           # 台股籌碼/估值（每小時隨日線刷新一次）
        self._chip_err = None       # 最近一次籌碼刷新的失敗原因（成功即清；見 _refresh_chip）
        self._bg_ts = 0.0           # 背景標的觸價巡檢的上次執行時刻（見 BG_QUOTE_SEC）
        self._shares_out = None     # 台股已發行股數（週轉率用；tw_chip 內部已日快取，這裡存最近一次結果）
        self._last_live_vol = None       # 即時成交量快取（Yahoo 該欄位偶爾單次缺漏，見 render 說明）
        self._last_live_vol_ts = 0.0
        self._stale_note = None          # 日線刷新失敗沿用舊快取時的畫面標註（成功刷新即清除）
        self._alert_state = {}           # E2 警戒武裝旗標（per symbol，session 記憶體）
        self._alert_banner = []          # 最近一批警戒事件顯示列（保留到下批事件覆蓋）

    def _fetch(self):
        """日線每小時重抓+重算一次（避免 60s 迴圈重抓 10y OHLC 與全套指標）；台股一併刷新籌碼。
        每小時刷新失敗但手上有快取 → 退回舊快取＋畫面標註（現價線本就獨立每 60s 抓，
        不因日線源短暫故障讓整頁被錯誤訊息取代），RETRY_REFRESH_SEC 後再試；
        無快取（首抓失敗）才拋給 run() 走重試/回上層。"""
        if self._daily_cache is None or (time.time() - self._daily_ts) >= self.DAILY_REFRESH_SEC:
            try:
                df = calculate_technical_indicators(fetch_ohlc(self.yahoo))
            except Exception as e:  # noqa: BLE001
                if self._daily_cache is None:
                    raise
                age_min = int((time.time() - self._daily_ts) // 60)
                self._stale_note = f"  ⚠ 日線刷新失敗（{str(e)[:60]}），沿用 {age_min} 分鐘前快取"
                self._daily_ts = time.time() - self.DAILY_REFRESH_SEC + self.RETRY_REFRESH_SEC
                return self._daily_cache
            if df is not None and not df.empty and len(df) >= 50:
                self._stale_note = None
                self._daily_cache = df
                self._daily_ts = time.time()
                if self.is_tw:
                    self._refresh_chip(df)
        return self._daily_cache

    def _refresh_chip(self, df):
        """台股籌碼/估值刷新（對齊最新日線日期；TWSE 全量檔為 EOD，盤中用最後交易日）。

        **例外一律吸收**：此處原本沒有 try/except，TWSE/TPEx 一有連線問題就會把例外
        丟出 `_fetch` → `run()` 當成「擷取失敗」走 transient 重試分支（整頁換成錯誤訊息、
        等 10 秒、fails+1），但那時**價格/趨勢/量能/K 線全都是好的**，只是籌碼那一塊拿不到。
        2026-08-11 以模擬 TWSE 逾時實測確認此路徑。
        改為：保留上一輪 `_chip`（TWSE 是 EOD 檔，舊一天仍是真資料，畫面的
        `籌碼資料截至 as_of` 會如實顯示是哪天），只記錯誤讓說明欄標註；下次日線刷新自動再試。"""
        from service.tw_chip import get_chip_bundle, get_shares_outstanding
        try:
            self._chip = get_chip_bundle(self.display, df.index[-1].strftime("%Y%m%d"))
            self._shares_out = get_shares_outstanding(self.display)
            self._chip_err = None
        except Exception as e:      # noqa: BLE001 — 籌碼源故障不得中斷監控（見 docstring）
            self._chip_err = str(e)[:70]

    def _background_events(self, sym_key, plans_all):
        """watch_plan 其餘標的的觸價巡檢（盯一檔不漏他檔）→ 事件列表。

        每 `BG_QUOTE_SEC` 才跑一輪、上限 `BG_QUOTE_MAX` 檔（限流理由見類別常數註解）；
        未屆期直接回 []（零網路請求）。**先剔本標的再取上限**，實際發數恆為 BG_QUOTE_MAX
        ——原本是先 `[:9]` 後 skip 本標的，本標的不在計畫內時會多打一發，與註解宣稱的
        「上限 8 檔」對不上。
        自成一個方法而非埋在 render 裡：render 要真實日線+終端機才跑得動，節流與上限
        這兩件事得能單獨驗（否則只能在測試裡複製一份迴圈，那等於驗自己寫的副本）。"""
        if time.time() - self._bg_ts < self.BG_QUOTE_SEC:
            return []
        self._bg_ts = time.time()
        events = []
        for sym, p in [(s, p) for s, p in plans_all.items() if s != sym_key][:self.BG_QUOTE_MAX]:
            try:
                q = fetch_live_quote(classify_symbol(sym)["yahoo"])
            except ValueError:
                continue
            if not q.get("price"):
                continue
            st_bg = self._alert_state.get(sym, {})
            evs, st_bg = check_price_events(p, q["price"], st_bg)
            self._alert_state[sym] = st_bg
            events += [dict(e, msg=e["msg"] + "〔背景標的〕") for e in evs]
        return events

    def render(self, df):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nxt = (datetime.datetime.now()
               + datetime.timedelta(seconds=self.REFRESH_SEC)).strftime("%H:%M:%S")
        row = df.iloc[-1]
        close = float(row["close"])
        trend = compute_trend_score(row, df)
        mom = _short_momentum(df)

        # 52 週高低（W-4：改用 high/low 欄含盤中影線，符合「52週高/低」一般理解口徑；
        # 原用 close 極值僅為「52週收盤高/低」，較窄）
        win = df.tail(252)
        hi = float(win["high"].max()) if "high" in win.columns else float(win["close"].max())
        lo = float(win["low"].min()) if "low" in win.columns else float(win["close"].min())
        pos = (close - lo) / (hi - lo) * 100 if hi > lo else 0.0

        header = [
            f"  通用監控儀表板 · {self.kind_label} · 趨勢方向（順勢軸）",
            f"  監測時間  {now}     邏輯來源  Cow 單一來源（core）",
            f"  代號      {self.display}（{self.yahoo}）     現價 {self.REFRESH_SEC}s｜日線每小時",
        ]
        # 現價：每 60s 抓 Yahoo 即時報價（盤中即時、盤後為收盤）；與每小時的日線+指標分離
        live = fetch_live_quote(self.yahoo)
        if live.get("price"):
            fr = live_quote_freshness(live, is_tw=self.is_tw)
            chg_txt = "" if fr["chg_pct"] is None else f"  {fr['chg_pct']:+.2f}%"
            price_line = f"  現價          {_fmt_price(live['price'])}{chg_txt}   {fr['label']}"
        else:
            price_line = f"  現價          {_fmt_price(close)}   （日線收盤）"
        # 「最新日線」若恰逢今日進行式棒（Yahoo 盤中即時更新的今日 1d bar），數字會跟「現價」
        # 完全重複（同日同價，看起來像多餘），改顯示前一個已結算交易日收盤，兩者才各有意義。
        # 最後一根是否為「今日進行式」：一處判定，日線行／量能分位／逃頂雷達共用
        # （量能側若不排除這根，會拿只累積到當下的當日量去比歷史整日量，見 vol_pctile）
        forming = len(df) >= 2 and is_daily_bar_forming(df.index[-1].date(), self.is_tw,
                                                        is_crypto=self.is_crypto)
        if forming:
            daily_date, daily_close = df.index[-2].date(), float(df.iloc[-2]["close"])
            daily_line = f"  前一交易日    {daily_date} 收 {_fmt_price(daily_close)}（{len(df)} 根）"
        else:
            daily_line = f"  最新日線      {df.index[-1].date()} 收 {_fmt_price(close)}（{len(df)} 根）"
        quote = [
            price_line,
            daily_line,
            f"  52週高/低     {_fmt_price(hi)} / {_fmt_price(lo)}   （位置 {pos:.0f}%）",
            f"  短線動能      {mom}",
        ]
        if self._stale_note:
            quote.append(self._stale_note)
        # 即時成交量（同一次 fetch_live_quote 內含，零額外網路成本）＋量能分位（個股自身歷史，複用
        # 台股高側「量能見頂」既有邏輯）＋週轉率（台股才有，需已發行股數，來源 TWSE/TPEx OpenAPI）。
        # Yahoo 回應偶爾單次缺漏 regularMarketVolume（價格欄位正常、僅此欄漏），resolve_live_volume
        # 退回快取避免每次刷新忽有忽無地閃爍（見該函式 docstring）。
        live_vol, vol_note = resolve_live_volume(
            live.get("volume"), self._last_live_vol, self._last_live_vol_ts, self.REFRESH_SEC)
        if live.get("volume"):
            self._last_live_vol, self._last_live_vol_ts = live["volume"], time.time()
        if live_vol:
            vol_line = f"  即時成交量    {live_vol:,.0f} 股{vol_note}"
            if self.is_tw and self._shares_out:
                vol_line += f"（週轉率 {live_vol / self._shares_out * 100:.2f}%）"
            quote.append(vol_line)
        # 口徑：近 N 日均量在「個股自身歷史同口徑均量」的排名（midrank 分位），**不是**量比倍數；
        # 盤中今日棒還沒累積完，均量只取已結算日（否則早盤把半天量拌進去，永遠顯示低分位）。
        # 分位與量比**並列兩行**：使用者會拿「今日量 ÷ N 日均量」去驗算分位（分母不同、不可
        # 互推），只寫分位必再被誤讀，故兩個問題各給各的數字（見 `vol_snapshot` docstring）。
        vol_snap = vol_snapshot(df, drop_last=forming)
        if vol_snap is not None:
            tail = "，今日未結算不計" if forming else ""
            since = df.index[0].date() if len(df) else ""
            quote.append(
                f"  量能分位      近{VOL_WINDOW}日均量 {vol_snap['ma']:,.0f} 股 → "
                f"{vol_snap['pctile'] * 100:.0f}分位"
                f"（母體＝{since} 起每日的{VOL_WINDOW}日均量 {vol_snap['n_pop']:,} 筆{tail}）")
            ratio_line = (f"  量比          近{VOL_WINDOW}日均量 ÷ 近{vol_snap['ref_window']}日均量"
                          f" {vol_snap['ratio']:.2f}x")
            if live_vol:
                ratio_line += (f"｜今日 {live_vol:,.0f} 股 ＝ 近{VOL_WINDOW}日均量的"
                               f" {live_vol / vol_snap['ma']:.2f}x")
            quote.append(ratio_line)
        # 時間序列動能（3/6/12M 報酬）— 參考訊號，未計入加權（待回測）
        quote += momentum_ref_rows(df)
        # 風控框架（ATR 停損 + 近 60 日支撐壓力風報比）— 支撐用近期低（股票無動態地板）
        quote += _atr_risk_rows(df, close, support=None)

        # 交易計畫（E1）：watch_plan.json 有本代號計畫才顯示（無計畫＝畫面與從前完全相同）。
        # 距離以當下有效價計（有即時報價用即時、否則日線收盤）；檔案壞掉只出警示行不中斷監控。
        eff_price = live["price"] if live.get("price") else close
        plan = get_plan(self.display)
        plans_all, plan_errs = load_plans_cached()   # 一次取用，背景巡檢（E2）也吃這份
        plan_rows = plan_panel_rows(plan, eff_price, fmt=_fmt_price) if plan else []
        plan_rows += [f"  ⚠ {e}" for e in plan_errs]
        _, trend_rows = _panel_trend(trend, "趨勢方向（順勢）",
                                     ("ma_structure", "macd", "slope", "adx"))
        ct_trend = f"趨勢  {trend[0]:+d}/±100  {_bar_signed(trend[0])}  {trend_meta(trend[0])[0]}"

        # 台股：完整逃頂/抄底（籌碼面）+ 三軸 composite；美股：僅趨勢×短線 stance
        top_rows = low_rows = None
        high = low = None               # E3 日誌快照用（fallback 分支無逃頂/抄底分數）
        ct_top = ct_low = ""
        if self.is_tw and self._chip is not None:
            high = compute_relative_high_tw(row, df, chip=self._chip, forming_last=forming)
            low = compute_relative_low_tw(row, df, chip=self._chip, forming_last=forming)
            _, top_rows = _panel(high, relative_high_tw_meta, 100, "逃頂訊號（台股籌碼）",
                                 ("technical", "valuation", "volume", "leverage", "institution",
                                  "tdcc", "vol_price"))
            _, low_rows = _panel(low, relative_low_tw_meta, 100, "抄底訊號（台股籌碼）",
                                 ("leverage", "technical", "institution", "valuation"))
            ct_top = f"逃頂  {high[0]}/100  {_bar(high[0], 100)}  {relative_high_tw_meta(high[0])[0]}"
            ct_low = f"抄底  {low[0]}/100  {_bar(low[0], 100)}  {relative_low_tw_meta(low[0])[0]}"
            # 三軸 composite：不傳 cycle_score（台股估值對底部是雜訊、且 max 僅 10 達不到 cycle 門檻）；
            # 由重配重後的 low_score≥60 驅動 value 分支（已含融資清洗權重30 這個校準最強底部維）。
            ct_comp, comp_rows, act = _composite_panel(trend[0], high[0], low[0])
            note = [f"  籌碼資料截至 {self._chip.get('as_of', '—')}（TWSE EOD；今日未收/連假自動取最近交易日）",
                    "  ⚠ 台股逃頂/抄底 v0.5〔2026-07-02 全市場 swing 回測拍板〕：逃頂靠估值(PE/PB絕對",
                    "     AUC~0.63)+量能見頂(0.648)+量價背離(0.566 已轉正式)；抄底靠融資清洗(0.564)。",
                    "     已移除：抄底大戶(AUC 0.422 方向反)、抄底量價/結構(0.50/0.52 雜訊)、逃頂結構(0.483)。",
                    "     法人為弱維(AUC<0.55) 僅參考。"]
            if self._chip_err:
                # 籌碼源本輪失敗但手上有舊 EOD 檔 → 續用並如實標註（上方 as_of 就是它的日期）
                note.insert(1, f"  ⚠ 本輪籌碼刷新失敗（{self._chip_err}），沿用上表日期資料；下次日線刷新自動再試")
        else:
            # C1（2026-07-04 拍板，2026-07-06 落地）：美股/其他非台股標的的逃頂/抄底通用軸
            # 面板已撤下（曾以 relative_high_us/relative_low_us 純 OHLCV 實作，2026-07-02
            # 家用網路回測 50 檔三維全近雜訊 AUC~0.5，權重未獲實證）。台股籌碼未就緒時亦走此分支。
            act = compute_trend_stance(trend[0], mom)
            _, comp_rows = _panel_stance(
                "操作", f"{act['emoji']} {act['action']}", act["detail"])
            ct_comp = f"操作  {act['emoji']} {act['action']}"
            if self.is_tw:
                why = f"（{self._chip_err}）" if self._chip_err else ""
                note = [f"  ⚠ 台股籌碼資料尚未就緒{why} → 暫僅通用軸（趨勢方向＋技術＋短線動能）。"]
            else:
                note = ["  ⚠ 美股/其他標的無籌碼/估值免費源 → 僅通用軸（趨勢方向＋技術＋短線動能）。",
                        "     股票版逃頂/抄底（融資融券/法人/期權IV）列為後續 Phase。"]

        # ── E2 警戒引擎：本標的觸價/訊號變化＋watch_plan 其餘標的背景觸價（盯一檔不漏他檔）──
        events = []
        sym_key = self.display.upper()
        st_sym = self._alert_state.get(sym_key, {})
        if plan is not None:
            evs, st_sym = check_price_events(plan, eff_price, st_sym)
            events += evs
        evs, st_sym = check_signal_change(sym_key, (act or {}).get("action_key"),
                                          (act or {}).get("action"), st_sym)
        events += evs
        self._alert_state[sym_key] = st_sym
        events += self._background_events(sym_key, plans_all)
        if events:
            self._alert_banner = banner_rows(events)   # 保留顯示直到下批事件覆蓋
            # E3：事件＋觸發當下訊號快照落日誌（背景標的無本畫面分數 → 快照僅本標的事件附）
            snap = {"trend": trend[0], "high": high[0] if high else None,
                    "low": low[0] if low else None, "action_key": (act or {}).get("action_key")}
            for e in events:
                journal_append(journal_record(e, snap if e["symbol"] == sym_key else None))
            notify_beep()
        self._last_eff_price = eff_price               # e 鍵執行標記（E3）記價用

        # 兩欄並排：操作｜趨勢、逃頂｜抄底（台股才有後者）— 與 BTC_WATCH 同一套版面（省垂直高度、一頁看完）
        panels = [(t, r) for t, r in ((ct_comp, comp_rows), (ct_trend, trend_rows),
                                      (ct_top, top_rows), (ct_low, low_rows)) if r]

        w_full = max((_dw(c) for c in (header + quote + note + plan_rows + self._alert_banner)),
                     default=40)
        col_need = max((_dw(t) for t, _ in panels), default=40) + 4   # +4：┌─ … ─┐ 邊距
        col_need = max(col_need, _MIN_COL_W)   # 每欄至少 _MIN_COL_W（拉寬 + 容對齊後子項）
        W = max(w_full, 2 * col_need + 2, _dw("即時行情") + 4)
        wl = (W - 2) // 2
        wr = (W - 2) - wl

        left = [_edge("╔", "═", "╗", W), _row(header[0], W, "║"), _edge("╠", "═", "╣", W)]
        left += [_row(h, W, "║") for h in header[1:]]
        left.append(_edge("╚", "═", "╝", W))

        if self._alert_banner:
            left += ["", _title("⚠ 警戒", W)]
            left += [_row(r, W) for r in self._alert_banner]
            left.append(_edge("└", "─", "┘", W))

        left += ["", _title("即時行情", W)]
        left += [_row(r, W) for r in quote]
        left.append(_edge("└", "─", "┘", W))

        if plan_rows:
            left += ["", _title("交易計畫", W)]
            left += [_row(r, W) for r in plan_rows]
            left.append(_edge("└", "─", "┘", W))

        for i in range(0, len(panels), 2):
            left.append("")
            if i + 1 < len(panels):
                left.extend(_pair_lines(panels[i], panels[i + 1], wl, wr))
            else:                                    # 奇數面板（美股僅操作+趨勢時不會發生）→ 佔全寬
                left.extend(_panel_block(panels[i][0], panels[i][1], W))

        left += ["", _title("說明", W)]
        for r in note:
            left += [_row(seg, W) for seg in _wrap_display(r, W)]
        left.append(_edge("└", "─", "┘", W))

        left += ["", f"  下次刷新 {nxt}    （b 重選代號｜e 記錄已執行｜q 結束）"]

        # 右側全高 K 線側欄（近 30 日日線，與 BitcoinMonitor 同一份 helper；
        # 終端機過窄/資料不足自動退回單欄）。均線組依市場類別取（台股 5/20/60/240 週月季年線，
        # 美股/幣 5/20/200 國際慣例——「根/年」不同，見 core.term_ui 的 KLINE_MAS 註解）。
        _print_with_kline(left, W, df, BitcoinMonitor.KLINE_DAYS,
                          mas=kline_mas_for(self.info["kind"]))

    def run(self):
        fails = 0                   # 連續暫時性失敗計數（成功一輪即歸零）
        while True:
            err = None
            try:
                df = self._fetch()
                insufficient = df is None or df.empty or len(df) < 50
            except RuntimeError as e:
                # fetch_ohlc「無資料」屬永久性失敗（代號打錯/已下市），重試只會無限撞 Yahoo
                print(f"[錯誤] {e}（代號可能有誤或已下市），回代號選單。")
                return "back"
            except Exception as e:  # noqa: BLE001
                insufficient, err = True, e
            if insufficient:
                fails += 1
                msg = f"擷取失敗（{self.yahoo}）：{err}" if err else f"資料不足（{self.yahoo}）"
                if fails >= self.MAX_TRANSIENT_FAILS:
                    print(f"{msg}，連續 {fails} 次，回代號選單。")
                    return "back"
                print(f"{msg}，10 秒後重試…（b 回選單｜q 結束）")
                cmd = interruptible_wait(10, nav=True)
                if cmd:
                    return cmd
                continue
            fails = 0
            try:
                self.render(df)
            except Exception as e:  # noqa: BLE001 — 渲染例外不終結監控（多為即時報價/終端邊角）
                print(f"畫面更新失敗：{e}，{self.REFRESH_SEC}s 後重試…")
            cmd = interruptible_wait(self.REFRESH_SEC, nav=True)
            if cmd == "exec":       # e 鍵：記「已依計畫執行」後繼續監控，不中斷（E3）
                sym = self.display.upper()
                journal_append(journal_record(
                    {"symbol": sym, "event": "executed", "price": getattr(self, "_last_eff_price", None),
                     "msg": "使用者標記：已依計畫執行"}))
                self._alert_banner = [f"  {datetime.datetime.now():%H:%M}  {sym}  📝 已記錄執行標記"]
                continue
            if cmd:
                return cmd          # 'back'（回上層重選）/ 'quit'（結束）


def _prompt_symbol() -> str:
    print("═" * 56)
    print("  Cow 通用監控  ·  輸入代號進入儀表板")
    print("  範例：BTCUSDT / ETHUSDT / SOLUSDT（幣）｜2330 / 0050（台股）｜QQQ / NVDA（美股）")
    print("═" * 56)
    return input("  代號 > ").strip()


def _build_monitor(info: dict):
    """依市場類別建對應 monitor（nav=True → 儀表板內可按鍵回上層/結束）。"""
    if info["is_btc"]:
        return BitcoinMonitor(nav=True)             # 完整逃頂五維＋抄底六維（原封不動）
    if info["kind"] == "crypto":
        # 非 BTC 幣對：完整逃頂/抄底，但停用 BTC 專屬維度（ETF/SOPR/BTC.D/四季論/礦工/冪律）
        # 可得天花板：逃頂 derivatives30+technical25+F&G10+事件3=68；抄底 cycle19+deriv20+tech20+F&G10+事件3=72
        return BitcoinMonitor(
            symbol=info["binance"], coin_symbol=info["coin"], is_btc=False,
            top_cap=68, low_cap=72, oi_unit=info["base"], nav=True,
            title=f"{info['base']} 加密雙向監控 · 逃頂(可得≤68) + 抄底(可得≤72)",
        )
    return UniversalMonitor(info)                   # 股票：通用軸（趨勢方向＋技術＋短線動能）


def main():
    """輸入代號 → 進儀表板；儀表板內按 b 回此處重選、q 結束（Ctrl+C 亦可強制結束）。"""
    argv_raw = sys.argv[1] if len(sys.argv) > 1 else None
    while True:
        try:
            raw = argv_raw or _prompt_symbol()
        except (KeyboardInterrupt, EOFError):       # 提示符按 Ctrl+C / Ctrl+Z → 乾淨結束不吐 traceback
            print("\n結束。")
            return
        argv_raw = None                             # 命令列代號只用第一次；回上層後一律重新提示
        try:
            info = classify_symbol(raw)
        except ValueError as e:
            print(f"[錯誤] {e}")
            continue
        kind_label = KIND_LABEL.get(info["kind"], info["kind"])
        if info["is_btc"]:
            label = "BTC 完整雙向雷達（逃頂五維＋抄底六維）"
        elif info["kind"] == "crypto":
            label = f"{info['base']} 加密雙向雷達（逃頂/抄底，停用 BTC 專屬維度）"
        else:
            label = f"{kind_label} 通用軸（趨勢方向）"
        print(f"\n→ 判定：{info['display']}（{kind_label}）→ {label}\n")

        try:
            time.sleep(0.8)
            cmd = _build_monitor(info).run()
        except KeyboardInterrupt:
            print("\n結束。")
            return
        if cmd == "quit":
            print("\n結束。")
            return
        # cmd == 'back'（或 None）→ 回到迴圈頂端重選代號


if __name__ == "__main__":
    main()
