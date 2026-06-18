"""
scripts/universal_watch_poc.py  ·  PoC

「把 BTC_WATCH.py 延伸到其他交易對 / 台股 / 美股」可行性的概念驗證。

證明的命題：BTC_WATCH 的評分軸中，**趨勢方向（core/trend_direction，±100）與技術指標
（core/indicators，除 AHR999 外）是 100% 通用 OHLC**，只要餵得到日線就能跑，與 BTC、
與 Binance 無關。本 PoC 用 yfinance 抓任意代號的日線（幣對 BTC-USD/ETH-USD、美股 AAPL、
台股 2330.TW 都同一條路），重用 Cow 既有 core 模組算出「趨勢方向 + 短線動能 + 關鍵技術值」。

刻意**不**搬 BTC 專屬維度（資金費率 / OI / 鏈上 / 四季論 / 礦工成本 / 冪律）——那些對股票
整段報廢、對其他幣對部分報廢，正是可行性報告的分界線。PoC 只驗證「通用軸」這條共同地基。

用法：
    python scripts/universal_watch_poc.py NVDA
    python scripts/universal_watch_poc.py 2330.TW
    python scripts/universal_watch_poc.py ETH-USD
    python scripts/universal_watch_poc.py            # 預設一次跑三類各一檔示範

資料源說明（實證踩坑）：原本想用 yfinance 套件，但它在公司/共享 IP 上會被 Yahoo 的 crumb
認證限流（YFRateLimitError 429，且預設 curl_cffi 路徑撞公司 SSL 攔截）。改直連 Yahoo
**v8 chart JSON 端點**（query1.finance.yahoo.com/v8/finance/chart/<symbol>），同一端點即可
取 BTC-USD / ETH-USD / AAPL / NVDA / 2330.TW 的日線，無金鑰、無 crumb、verify=False 可過
公司 SSL——這正是「股票延伸的通用資料源」最務實的選擇，也是報告的資料源結論之一。
"""
import os
import sys
import math

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 單一真實來源：重用 Cow core / service（與 BTC_WATCH.py 相同的 path import 慣例）──
_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _COW not in sys.path:
    sys.path.insert(0, _COW)
from core.indicators import calculate_technical_indicators          # noqa: E402
from core.trend_direction import compute_trend_score, trend_meta    # noqa: E402
from service.ohlc_universal import fetch_ohlc                        # noqa: E402


def _short_momentum(df):
    """近 7 根報酬 + 價 vs EMA_20 + RSI_14（與 BTC_WATCH._short_momentum 同義，通用）。"""
    if df is None or len(df) < 8:
        return "—"
    close = float(df["close"].iloc[-1])
    prev7 = float(df["close"].iloc[-8])
    ret7 = (close / prev7 - 1) * 100 if prev7 else 0.0

    def _last(col):
        if col not in df.columns:
            return None
        v = df[col].iloc[-1]
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    ema20, rsi = _last("EMA_20"), _last("RSI_14")
    above = None if ema20 is None else close > ema20
    if ret7 > 0 and above is True:
        lbl = "🟢 短線偏多"
    elif ret7 < 0 and above is False:
        lbl = "🔴 短線偏空"
    else:
        lbl = "⚪ 短線中性"
    parts = [f"近7根 {ret7:+.1f}%"]
    if above is not None:
        parts.append("價>EMA20" if above else "價<EMA20")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    return f"{lbl}  " + "｜".join(parts)


def _bar_signed(net):
    """有號淨方向分置中條（沿用 BTC_WATCH 風格）。"""
    mag = int(round(min(abs(net), 100) / 100 * 5))
    if net >= 0:
        return "░" * 5 + "│" + "█" * mag + "░" * (5 - mag)
    return "░" * (5 - mag) + "█" * mag + "│" + "░" * 5


def analyze(symbol: str):
    df = fetch_ohlc(symbol)
    df = calculate_technical_indicators(df)
    row = df.iloc[-1]
    net, sig = compute_trend_score(row, df)
    level, _, action = trend_meta(net)

    print(f"\n{'═' * 60}")
    print(f"  通用監控 PoC ── {symbol}    （資料源 Yahoo v8 chart，與 Binance 無關）")
    print(f"{'═' * 60}")
    print(f"  最新日期      {df.index[-1].date()}    共 {len(df)} 根日線")
    print(f"  收盤          {float(row['close']):,.2f}")
    print(f"  短線動能      {_short_momentum(df)}")
    print(f"{'─' * 60}")
    print(f"  趨勢方向      {net:+d}/±100  {_bar_signed(net)}  {level}")
    for d in ("ma_structure", "macd", "slope", "adx"):
        s = sig[d]
        print(f"    {s['score']:+3d}/±{s['max']:<2}  {s['label']}")
    print(f"    → {action}")
    print(f"{'─' * 60}")
    print("  ✅ 通用軸（趨勢方向 + 技術指標）跨市場可跑，無需任何 BTC/Binance 專屬資料。")
    return net


def main():
    args = sys.argv[1:]
    symbols = args if args else ["ETH-USD", "AAPL", "2330.TW"]
    if not args:
        print("（未指定代號 → 示範跑：加密 ETH-USD / 美股 AAPL / 台股 2330.TW）")
    ok = 0
    for sym in symbols:
        try:
            analyze(sym)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"\n[失敗] {sym}：{e}")
    print(f"\n{'═' * 60}\n  完成 {ok}/{len(symbols)} 檔。"
          f"  本 PoC 證明：通用 OHLC 軸可直接服務幣對 / 美股 / 台股。\n{'═' * 60}")


if __name__ == "__main__":
    main()
