"""
scripts/backtest_radar_at_date.py
用「目前的」逃頂/抄底算法（core.relative_high / core.relative_low）回測某個歷史日期當天的分數。

用法：
    python scripts/backtest_radar_at_date.py 2026-05-06          # 逃頂＋抄底都印
    python scripts/backtest_radar_at_date.py 2026-06-05 2026-05-06 ...   # 多個日期

⚠️ 歷史可重建性（重要）：分數的部分輸入無法對過去日期忠實重建，回測時一律給 None（灰燈/0 分），
故「可得天花板」低於即時版：
  - technical（背離/RSI）、cycle（Mayer/200週/冪律）：純價格，**可重建** ✅
  - funding 資金費率：Binance /fapi/v1/fundingRate 有歷史 → **可重建** ✅
  - F&G 恐懼貪婪：alternative.me 有歷史（~400 天）→ **可重建** ✅
  - OI 分位/清洗：Binance openInterestHist 僅留 ~30 天 → 過去日期**無法重建** ❌（None）
  - onchain(ETF/SOPR)、BTC.D、macro：歷史源不易重建 → 一律 None ❌
回測可得：逃頂 ≤55（derivatives 僅 funding20 + technical25 + F&G10）；
         抄底 ≤65（cycle25 + funding10 + technical20 + F&G10）。
"""
import os
import sys
import datetime as dt

import urllib3
import requests
import pandas as pd

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _COW not in sys.path:
    sys.path.insert(0, _COW)
from core.indicators import calculate_technical_indicators, calculate_ahr999   # noqa: E402
from core.bear_bottom import calculate_bear_bottom_indicators                  # noqa: E402
from core.relative_high import compute_escape_top_score, escape_top_meta       # noqa: E402
from core.relative_low import compute_relative_low_score, relative_low_meta    # noqa: E402

_UTC = dt.timezone.utc
_FAPI = "https://fapi.binance.com/fapi/v1"


def _session():
    s = requests.Session()
    s.verify = False
    return s


def fetch_daily(s, limit=1500):
    """日線 OHLCV（limit 1500≈4年，足夠 SMA200週=1400日與 Mayer=730日），算好全套指標。"""
    r = s.get(f"{_FAPI}/klines", params={"symbol": "BTCUSDT", "interval": "1d", "limit": limit},
              timeout=20).json()
    rows = [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in r]
    idx = pd.to_datetime([k[0] for k in r], unit="ms")
    df = pd.DataFrame(rows, index=idx)
    df = calculate_technical_indicators(df)
    df = calculate_ahr999(df)
    df = calculate_bear_bottom_indicators(df)
    return df


def funding_at(s, date):
    """當日最後一次結算的 8h 資金費率（%）。Binance /fundingRate 有長期歷史。"""
    start = int(dt.datetime(date.year, date.month, date.day, tzinfo=_UTC).timestamp() * 1000)
    end = start + 86_400_000
    r = s.get(f"{_FAPI}/fundingRate",
              params={"symbol": "BTCUSDT", "startTime": start, "endTime": end, "limit": 10},
              timeout=15).json()
    return float(r[-1]["fundingRate"]) * 100 if r else None


def fetch_fng_map(s, limit=400):
    """{date: 恐懼貪婪指數}（alternative.me 歷史 ~400 天）。多日期回測時只抓一次。"""
    r = s.get("https://api.alternative.me/fng/", params={"limit": limit, "format": "json"},
              timeout=15).json()
    return {dt.datetime.fromtimestamp(int(d["timestamp"]), _UTC).date(): float(d["value"])
            for d in r.get("data", [])}


def _print_panel(name, result, meta_fn, cap, dims):
    score, sig = result
    level, _, action = meta_fn(score)
    print(f"  {name}  {score}/100   （回測可得≤{cap}）   {level}")
    for d in dims:
        print(f"    {sig[d]['score']:>2}/{sig[d]['max']:<2}  {sig[d]['label']}")
    print(f"    → {action}")


def backtest(s, df, date, fng_map):
    df_t = df.loc[:f"{date} 23:59"]
    if df_t.empty:
        print(f"[{date}] 無日線資料"); return
    row = df_t.iloc[-1]
    close = float(row["close"])
    funding = funding_at(s, date)
    fng = fng_map.get(date)

    # 歷史不可重建的維度一律 None（OI/onchain/BTC.D/macro）
    common = dict(funding_8h=funding, oi_stats=None, fng=fng,
                  etf_summary=None, sopr=None, btc_d_trend=None, macro=None)
    top = compute_escape_top_score(row, df_t, **common)
    low = compute_relative_low_score(row, df_t, **common)

    print(f"\n{'═' * 64}")
    print(f"  回測日 {date}   收盤 ${close:,.0f}   "
          f"資費 {('—' if funding is None else f'{funding:.4f}%/8h')}   F&G {('—' if fng is None else int(fng))}")
    print(f"{'═' * 64}")
    _print_panel("逃頂訊號（出貨）", top, escape_top_meta, 55,
                 ("derivatives", "technical", "onchain", "sentiment", "macro"))
    print(f"  {'─' * 60}")
    _print_panel("抄底訊號（進場）", low, relative_low_meta, 65,
                 ("cycle", "derivatives", "technical", "sentiment", "onchain", "macro"))


def main():
    dates = sys.argv[1:] or ["2026-05-06", "2026-06-05"]
    s = _session()
    df = fetch_daily(s)
    fng_map = fetch_fng_map(s)
    print(f"日線 {len(df)} 根，{df.index[0].date()} ~ {df.index[-1].date()}")
    for d in dates:
        backtest(s, df, dt.date.fromisoformat(d), fng_map)
    print(f"\n{'═' * 64}")
    print("  註：OI/onchain/BTC.D/macro 歷史不可重建 → 灰燈(0)；分數為「可重建子集」，"
          "故與即時版（可得≤93）不可直接相比，看絕對高低與維度分佈即可。")


if __name__ == "__main__":
    main()
