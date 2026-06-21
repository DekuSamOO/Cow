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
from core.trend_direction import compute_trend_score                # noqa: E402
from core.action_ensemble import compute_trend_stance, compute_composite_action  # noqa: E402
from core.relative_high_tw import compute_relative_high_tw, relative_high_tw_meta  # noqa: E402
from core.relative_low_tw import compute_relative_low_tw, relative_low_tw_meta     # noqa: E402
from service.ohlc_universal import (classify_symbol, fetch_ohlc,            # noqa: E402
                                    fetch_live_quote, live_quote_freshness, KIND_LABEL)
# 重用 BTC_WATCH 既有的畫框 / 面板 / 等待 helper（單一真實來源，不重造）
from BTC_WATCH import (BitcoinMonitor, _title, _row, _edge, _dw, _panel,  # noqa: E402
                       _short_momentum, _panel_trend, _panel_stance, interruptible_wait)


def _fmt_price(v: float) -> str:
    """跨標的價格格式：大數兩位、個位數多給小數（小市值幣）。"""
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


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

    def _fetch(self):
        """日線每小時重抓+重算一次（避免 60s 迴圈重抓 2y OHLC 與全套指標）；台股一併刷新籌碼。"""
        if self._daily_cache is None or (time.time() - self._daily_ts) >= self.DAILY_REFRESH_SEC:
            df = calculate_technical_indicators(fetch_ohlc(self.yahoo))
            if df is not None and not df.empty and len(df) >= 50:
                self._daily_cache = df
                self._daily_ts = time.time()
                if self.is_tw:
                    # 籌碼/估值對齊最新日線日期（TWSE 全量檔為 EOD，盤中用最後交易日）
                    from service.tw_chip import get_chip_bundle
                    self._chip = get_chip_bundle(self.display, df.index[-1].strftime("%Y%m%d"))
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
        trend_title, trend_rows = _panel_trend(trend, "趨勢方向（順勢）",
                                               ("ma_structure", "macd", "slope", "adx"))

        # 台股：完整逃頂/抄底（籌碼面）+ 三軸 composite；美股：僅趨勢×短線 stance
        top_title = top_rows = low_title = low_rows = None
        if self.is_tw and self._chip is not None:
            high = compute_relative_high_tw(row, df, chip=self._chip)
            low = compute_relative_low_tw(row, df, chip=self._chip)
            top_title, top_rows = _panel(high, relative_high_tw_meta, 100, "逃頂訊號（台股籌碼）",
                                         ("technical", "institution", "leverage", "valuation", "tdcc"))
            low_title, low_rows = _panel(low, relative_low_tw_meta, 100, "抄底訊號（台股籌碼）",
                                         ("valuation", "technical", "leverage", "institution", "tdcc"))
            # 三軸 composite（cycle 用台股估值深跌子分，max 25 同尺度）
            act = compute_composite_action(trend[0], high[0], low[0],
                                           low[1]["valuation"]["score"])
            comp_title, comp_rows = _panel_stance(
                "操作訊號（三軸融合）", f"{act['emoji']} {act['action']}", act["detail"])
            comp_rows.append(f"     {act['pos_label']}")
            note = ["  ⚠ 台股逃頂/抄底為 v0.1〔絕對值起步・未擬合〕：PE/PB 用絕對值非分位、",
                    "     籌碼閾值為專家起點，待累積台股歷史回測校準。"]
        else:
            st = compute_trend_stance(trend[0], mom)
            comp_title, comp_rows = _panel_stance(
                "操作訊號（趨勢×短線）", f"{st['emoji']} {st['action']}", st["detail"])
            note = ["  ⚠ 美股：個股槓桿/法人/IV 無免費源 → 僅通用軸（趨勢方向＋技術＋短線動能）。"]

        panels_rows = (top_rows or []) + (low_rows or [])
        content_w = max((_dw(c) for c in (header + quote + trend_rows + comp_rows + panels_rows + note)), default=40)
        W = max(content_w, _dw(trend_title) + 4, _dw(comp_title) + 4,
                _dw(top_title or "") + 4, _dw(low_title or "") + 4, _dw("即時行情") + 4) + 2

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

        print()
        print(_title(comp_title, W))
        for r in comp_rows:
            print(_row(r, W))
        print(_edge("└", "─", "┘", W))

        print()
        print(_title(trend_title, W))
        for r in trend_rows:
            print(_row(r, W))
        print(_edge("└", "─", "┘", W))

        for ttl, rws in ((top_title, top_rows), (low_title, low_rows)):
            if rws:
                print()
                print(_title(ttl, W))
                for r in rws:
                    print(_row(r, W))
                print(_edge("└", "─", "┘", W))

        print()
        print(_title("說明", W))
        for r in note:
            print(_row(r, W))
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
