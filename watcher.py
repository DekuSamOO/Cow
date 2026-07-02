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
from core.relative_high_tw import compute_relative_high_tw, relative_high_tw_meta, _vol_pctile  # noqa: E402
from core.relative_low_tw import compute_relative_low_tw, relative_low_tw_meta     # noqa: E402
from core.relative_high_us import compute_relative_high_us, relative_high_us_meta  # noqa: E402
from core.relative_low_us import compute_relative_low_us, relative_low_us_meta     # noqa: E402
from service.ohlc_universal import (classify_symbol, fetch_ohlc,            # noqa: E402
                                    fetch_live_quote, live_quote_freshness, KIND_LABEL)
# 重用 BTC_WATCH 既有的畫框 / 面板 / 等待 helper（單一真實來源，不重造）
from core.momentum import momentum_ref_rows                             # noqa: E402
from BTC_WATCH import (BitcoinMonitor, _title, _row, _edge, _dw, _bar, _bar_signed,  # noqa: E402
                       _panel, _short_momentum, _panel_trend, _panel_stance,
                       interruptible_wait, _atr_risk_rows, _print_pair, _wrap_display)


def _fmt_price(v: float) -> str:
    """跨標的價格格式：大數兩位、個位數多給小數（小市值幣）。"""
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


def _composite_panel(trend0: int, high0: int, low0: int):
    """三軸 composite 操作面板（趨勢方向＋逃頂＋抄底）→ (ct_comp, comp_rows)。
    台股／美股分支邏輯完全相同（皆不傳 cycle_score，見呼叫端註解），抽共用避免複製貼上。"""
    act = compute_composite_action(trend0, high0, low0)
    _, comp_rows = _panel_stance("操作", f"{act['emoji']} {act['action']}", act["detail"])
    comp_rows.append(f"     {act['pos_label']}")
    ct_comp = f"操作  {act['emoji']} {act['action']}"
    return ct_comp, comp_rows


class UniversalMonitor:
    """非 BTC 標的的通用監控（趨勢方向＋技術＋短線動能）。資料走 Yahoo v8 chart。"""

    REFRESH_SEC = 60            # 畫面刷新（時鐘）
    DAILY_REFRESH_SEC = 3600    # 日線重抓+重算間隔（日 K 不會分鐘級變動，比照 BitcoinMonitor）

    def __init__(self, info: dict):
        self.info = info            # {kind, display, yahoo, is_btc}
        self.display = info["display"]
        self.yahoo = info["yahoo"]
        self.kind_label = KIND_LABEL.get(info["kind"], info["kind"])
        self.is_tw = info["kind"] == "tw_stock"   # 台股有逃頂/抄底籌碼面板；美股僅通用軸
        self._daily_cache = None
        self._daily_ts = 0.0
        self._chip = None           # 台股籌碼/估值（每小時隨日線刷新一次）
        self._shares_out = None     # 台股已發行股數（週轉率用；tw_chip 內部已日快取，這裡存最近一次結果）

    def _fetch(self):
        """日線每小時重抓+重算一次（避免 60s 迴圈重抓 2y OHLC 與全套指標）；台股一併刷新籌碼。"""
        if self._daily_cache is None or (time.time() - self._daily_ts) >= self.DAILY_REFRESH_SEC:
            df = calculate_technical_indicators(fetch_ohlc(self.yahoo))
            if df is not None and not df.empty and len(df) >= 50:
                self._daily_cache = df
                self._daily_ts = time.time()
                if self.is_tw:
                    # 籌碼/估值對齊最新日線日期（TWSE 全量檔為 EOD，盤中用最後交易日）
                    from service.tw_chip import get_chip_bundle, get_shares_outstanding
                    self._chip = get_chip_bundle(self.display, df.index[-1].strftime("%Y%m%d"))
                    self._shares_out = get_shares_outstanding(self.display)
        return self._daily_cache

    def render(self, df):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nxt = (datetime.datetime.now()
               + datetime.timedelta(seconds=self.REFRESH_SEC)).strftime("%H:%M:%S")
        row = df.iloc[-1]
        close = float(row["close"])
        trend = compute_trend_score(row, df)
        mom = _short_momentum(df)

        # 52 週高低（純 OHLC，給股票相對位置脈絡）
        win = df["close"].tail(252)
        hi, lo = float(win.max()), float(win.min())
        pos = (close - lo) / (hi - lo) * 100 if hi > lo else 0.0

        header = [
            f"  通用監控儀表板 · {self.kind_label} · 趨勢方向（順勢軸）",
            f"  監測時間  {now}     邏輯來源  Cow 單一來源（core）",
            f"  代號      {self.display}（{self.yahoo}）     現價 {self.REFRESH_SEC}s｜日線每小時",
        ]
        # 現價：每 60s 抓 Yahoo 即時報價（盤中即時、盤後為收盤）；與每小時的日線+指標分離
        live = fetch_live_quote(self.yahoo)
        if live.get("price"):
            fr = live_quote_freshness(live)
            chg_txt = "" if fr["chg_pct"] is None else f"  {fr['chg_pct']:+.2f}%"
            price_line = f"  現價          {_fmt_price(live['price'])}{chg_txt}   {fr['label']}"
        else:
            price_line = f"  現價          {_fmt_price(close)}   （日線收盤）"
        quote = [
            price_line,
            f"  最新日線      {df.index[-1].date()} 收 {_fmt_price(close)}（{len(df)} 根）",
            f"  52週高/低     {_fmt_price(hi)} / {_fmt_price(lo)}   （位置 {pos:.0f}%）",
            f"  短線動能      {mom}",
        ]
        # 即時成交量（同一次 fetch_live_quote 內含，零額外網路成本）＋量能分位（個股自身歷史，複用
        # 台股高側「量能見頂」既有邏輯）＋週轉率（台股才有，需已發行股數，來源 TWSE/TPEx OpenAPI）。
        live_vol = live.get("volume")
        if live_vol:
            vol_line = f"  即時成交量    {live_vol:,.0f} 股"
            if self.is_tw and self._shares_out:
                vol_line += f"（週轉率 {live_vol / self._shares_out * 100:.2f}%）"
            quote.append(vol_line)
        pct = _vol_pctile(df)
        if pct is not None:
            quote.append(f"  量能分位      個股自身 {pct * 100:.0f}分位（近期日均量比較）")
        # 時間序列動能（3/6/12M 報酬）— 參考訊號，未計入加權（待回測）
        quote += momentum_ref_rows(df)
        # 風控框架（ATR 停損 + 近 60 日支撐壓力風報比）— 支撐用近期低（股票無動態地板）
        quote += _atr_risk_rows(df, close, support=None)
        _, trend_rows = _panel_trend(trend, "趨勢方向（順勢）",
                                     ("ma_structure", "macd", "slope", "adx"))
        ct_trend = f"趨勢  {trend[0]:+d}/±100  {_bar_signed(trend[0])}  {trend_meta(trend[0])[0]}"

        # 台股：完整逃頂/抄底（籌碼面）+ 三軸 composite；美股：僅趨勢×短線 stance
        top_rows = low_rows = None
        ct_top = ct_low = ""
        if self.is_tw and self._chip is not None:
            high = compute_relative_high_tw(row, df, chip=self._chip)
            low = compute_relative_low_tw(row, df, chip=self._chip)
            _, top_rows = _panel(high, relative_high_tw_meta, 100, "逃頂訊號（台股籌碼）",
                                 ("technical", "valuation", "volume", "leverage", "institution",
                                  "tdcc", "vol_price", "structure"))
            _, low_rows = _panel(low, relative_low_tw_meta, 100, "抄底訊號（台股籌碼）",
                                 ("leverage", "technical", "institution", "tdcc", "valuation",
                                  "vol_price", "structure"))
            ct_top = f"逃頂  {high[0]}/100  {_bar(high[0], 100)}  {relative_high_tw_meta(high[0])[0]}"
            ct_low = f"抄底  {low[0]}/100  {_bar(low[0], 100)}  {relative_low_tw_meta(low[0])[0]}"
            # 三軸 composite：不傳 cycle_score（台股估值對底部是雜訊、且 max 僅 10 達不到 cycle 門檻）；
            # 由重配重後的 low_score≥60 驅動 value 分支（已含融資清洗權重30 這個校準最強底部維）。
            ct_comp, comp_rows = _composite_panel(trend[0], high[0], low[0])
            note = [f"  籌碼資料截至 {self._chip.get('as_of', '—')}（TWSE EOD；今日未收/連假自動取最近交易日）",
                    "  ⚠ 台股逃頂/抄底 v0.4〔2026-06 swing 回測校準＋2026-07 疊加新維〕：逃頂靠估值",
                    "     (PE/PB絕對 AUC~0.63)、抄底靠融資清洗(AUC 0.564)；法人/TDCC 為弱維(AUC<0.55)、",
                    "     量價背離/結構轉折為未擬合新維（規則式，尚未回測），皆僅參考。"]
        elif not self.is_tw:
            # 美股：無籌碼/估值免費源，但量價背離＋結構轉折＋技術背離皆純 OHLCV → 通用軸也能有逃頂/抄底
            high = compute_relative_high_us(row, df)
            low = compute_relative_low_us(row, df)
            _, top_rows = _panel(high, relative_high_us_meta, 100, "逃頂訊號（美股通用軸）",
                                 ("technical", "vol_price", "structure"))
            _, low_rows = _panel(low, relative_low_us_meta, 100, "抄底訊號（美股通用軸）",
                                 ("technical", "vol_price", "structure"))
            ct_top = f"逃頂  {high[0]}/100  {_bar(high[0], 100)}  {relative_high_us_meta(high[0])[0]}"
            ct_low = f"抄底  {low[0]}/100  {_bar(low[0], 100)}  {relative_low_us_meta(low[0])[0]}"
            ct_comp, comp_rows = _composite_panel(trend[0], high[0], low[0])
            note = ["  ⚠ 美股逃頂/抄底 v0.1〔2026-07 新建〕：個股槓桿/法人/IV 無免費源，改用純 OHLCV",
                    "     通用軸（技術背離+量價背離+結構轉折）。全數規則式，尚未在美股資料上跑過",
                    "     回測，權重為專家經驗值，僅供參考。"]
        else:
            st = compute_trend_stance(trend[0], mom)
            _, comp_rows = _panel_stance(
                "操作", f"{st['emoji']} {st['action']}", st["detail"])
            ct_comp = f"操作  {st['emoji']} {st['action']}"
            note = ["  ⚠ 台股籌碼資料尚未就緒 → 暫僅通用軸（趨勢方向＋技術＋短線動能）。"]

        # 兩欄並排：操作｜趨勢、逃頂｜抄底（台股才有後者）— 與 BTC_WATCH 同一套版面（省垂直高度、一頁看完）
        panels = [(t, r) for t, r in ((ct_comp, comp_rows), (ct_trend, trend_rows),
                                      (ct_top, top_rows), (ct_low, low_rows)) if r]

        w_full = max((_dw(c) for c in (header + quote + note)), default=40)
        col_need = max((_dw(t) for t, _ in panels), default=40) + 4   # +4：┌─ … ─┐ 邊距
        W = max(w_full, 2 * col_need + 2, _dw("即時行情") + 4)
        wl = (W - 2) // 2
        wr = (W - 2) - wl

        print(_edge("╔", "═", "╗", W))
        print(_row(header[0], W, "║"))
        print(_edge("╠", "═", "╣", W))
        for h in header[1:]:
            print(_row(h, W, "║"))
        print(_edge("╚", "═", "╝", W))

        print()
        print(_title("即時行情", W))
        for r in quote:
            print(_row(r, W))
        print(_edge("└", "─", "┘", W))

        for i in range(0, len(panels), 2):
            print()
            if i + 1 < len(panels):
                _print_pair(panels[i], panels[i + 1], wl, wr)
            else:                                    # 奇數面板（美股僅操作+趨勢時不會發生）→ 佔全寬
                t, rows = panels[i]
                print(_title(t, W))
                for r in rows:
                    for seg in _wrap_display(r, W):
                        print(_row(seg, W))
                print(_edge("└", "─", "┘", W))

        print()
        print(_title("說明", W))
        for r in note:
            for seg in _wrap_display(r, W):
                print(_row(seg, W))
        print(_edge("└", "─", "┘", W))

        print(f"\n  下次刷新 {nxt}    （b 重選代號｜q 結束）")

    def run(self):
        while True:
            try:
                df = self._fetch()
                if df is None or df.empty or len(df) < 50:
                    print(f"資料不足（{self.yahoo}），10 秒後重試…")
                    time.sleep(10)
                    continue
                self.render(df)
            except Exception as e:  # noqa: BLE001
                print(f"擷取失敗（{self.yahoo}）：{e}\n10 秒後重試…")
                time.sleep(10)
                continue
            cmd = interruptible_wait(self.REFRESH_SEC, nav=True)
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
        raw = argv_raw or _prompt_symbol()
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
        time.sleep(0.8)

        try:
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
