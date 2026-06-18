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
from service.ohlc_universal import classify_symbol, fetch_ohlc, KIND_LABEL  # noqa: E402
# 重用 BTC_WATCH 既有的畫框 / 面板 helper（單一真實來源，不重造）
from BTC_WATCH import (BitcoinMonitor, _title, _row, _edge, _dw,     # noqa: E402
                       _short_momentum, _panel_trend)


def _fmt_price(v: float) -> str:
    """跨標的價格格式：大數兩位、個位數多給小數（小市值幣）。"""
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


class UniversalMonitor:
    """非 BTC 標的的通用監控（趨勢方向＋技術＋短線動能）。資料走 Yahoo v8 chart。"""

    REFRESH_SEC = 60

    def __init__(self, info: dict):
        self.info = info            # {kind, display, yahoo, is_btc}
        self.display = info["display"]
        self.yahoo = info["yahoo"]
        self.kind_label = KIND_LABEL.get(info["kind"], info["kind"])

    def _fetch(self):
        df = fetch_ohlc(self.yahoo)
        return calculate_technical_indicators(df)

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
            f"  代號      {self.display}（{self.yahoo}）     刷新週期  {self.REFRESH_SEC} 秒",
        ]
        quote = [
            f"  最新日期      {df.index[-1].date()}   共 {len(df)} 根日線",
            f"  收盤          {_fmt_price(close)}",
            f"  52週高/低     {_fmt_price(hi)} / {_fmt_price(lo)}   （位置 {pos:.0f}%）",
            f"  短線動能      {mom}",
        ]
        trend_title, trend_rows = _panel_trend(trend, "趨勢方向（順勢）",
                                               ("ma_structure", "macd", "slope", "adx"))
        note = [
            "  ⚠ 非 BTC 標的：逃頂/抄底雙向雷達需加密永續(資金費率/OI)與 BTC 鏈上/減半",
            "     週期資料，股票無對應 → 本版僅通用軸（股票版逃頂抄底列為後續 Phase）。",
        ]

        content_w = max((_dw(c) for c in (header + quote + trend_rows + note)), default=40)
        W = max(content_w, _dw(trend_title) + 4, _dw("即時行情") + 4) + 2

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
        print(_title(trend_title, W))
        for r in trend_rows:
            print(_row(r, W))
        print(_edge("└", "─", "┘", W))

        print()
        print(_title("說明", W))
        for r in note:
            print(_row(r, W))
        print(_edge("└", "─", "┘", W))

        print(f"\n  下次刷新 {nxt}    （Ctrl+C 結束）")

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
            time.sleep(self.REFRESH_SEC)


def _prompt_symbol() -> str:
    print("═" * 56)
    print("  Cow 通用監控  ·  輸入代號進入儀表板")
    print("  範例：BTCUSDT（幣）｜ETHUSDT（幣）｜2330（台股）｜QQQ（美股）")
    print("═" * 56)
    return input("  代號 > ").strip()


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else _prompt_symbol()
    try:
        info = classify_symbol(raw)
    except ValueError as e:
        print(f"[錯誤] {e}")
        return
    label = "BTC 完整雙向雷達" if info["is_btc"] else f"{KIND_LABEL.get(info['kind'])} 通用軸"
    print(f"\n→ 判定：{info['display']}（{KIND_LABEL.get(info['kind'])}）→ {label}\n")
    time.sleep(0.8)
    if info["is_btc"]:
        BitcoinMonitor().run()          # 完整逃頂五維＋抄底六維（原封不動）
    else:
        UniversalMonitor(info).run()


if __name__ == "__main__":
    main()
