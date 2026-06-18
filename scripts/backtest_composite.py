"""
scripts/backtest_composite.py
2 年回測 core.action_ensemble 三軸合成行動（含 cycle 深跌補強）— 歸納/驗證用（非調參）。

逐日算 趨勢方向 / 逃頂 / 抄底 / cycle → composite stance，印出每次「訊號轉換」當日價格與
其後 30/60 日漲跌，並統計各訊號狀態的前瞻報酬與佔比，驗證訊號是否落在合理時點。

歷史可重建性：趨勢/cycle/技術=純價格可重建；F&G、funding 各幾次 API 補齊兩年；
OI/onchain/macro 歷史不可重建→None（composite 主力本就靠趨勢＋cycle，不依賴這些）。
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
from core.relative_high import compute_escape_top_score                        # noqa: E402
from core.relative_low import compute_relative_low_score                       # noqa: E402
from core.trend_direction import compute_trend_score                           # noqa: E402
from core.action_ensemble import compute_composite_action                      # noqa: E402

_UTC = dt.timezone.utc
_FAPI = "https://fapi.binance.com/fapi/v1"


def _session():
    s = requests.Session(); s.verify = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def fetch_daily(s, limit=1500):
    r = s.get(f"{_FAPI}/klines", params={"symbol": "BTCUSDT", "interval": "1d", "limit": limit},
              timeout=20).json()
    df = pd.DataFrame(
        [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
          "close": float(k[4]), "volume": float(k[5])} for k in r],
        index=pd.to_datetime([k[0] for k in r], unit="ms"))
    df = calculate_bear_bottom_indicators(calculate_ahr999(calculate_technical_indicators(df)))
    return df


def fetch_funding_map(s, days=760):
    """date → 當日最後一次 8h 資金費率(%)。分頁抓滿區間。"""
    out, start = {}, int((dt.datetime.now(_UTC) - dt.timedelta(days=days)).timestamp() * 1000)
    now = int(dt.datetime.now(_UTC).timestamp() * 1000)
    while start < now:
        r = s.get(f"{_FAPI}/fundingRate",
                  params={"symbol": "BTCUSDT", "startTime": start, "limit": 1000}, timeout=20).json()
        if not r:
            break
        for x in r:
            d = dt.datetime.fromtimestamp(x["fundingTime"] / 1000, _UTC).date()
            out[d] = float(x["fundingRate"]) * 100         # 同日後寫覆蓋 → 留當日最後一筆
        start = r[-1]["fundingTime"] + 1
        if len(r) < 1000:
            break
    return out


def fetch_fng_map(s, limit=800):
    r = s.get("https://api.alternative.me/fng/", params={"limit": limit, "format": "json"},
              timeout=20).json()
    return {dt.datetime.fromtimestamp(int(d["timestamp"]), _UTC).date(): float(d["value"])
            for d in r.get("data", [])}


def main():
    s = _session()
    df = fetch_daily(s)
    funding = fetch_funding_map(s)
    fng = fetch_fng_map(s)
    print(f"日線 {len(df)} 根；funding {len(funding)} 天；F&G {len(fng)} 天")

    closes = df["close"].values
    recs = []
    start_i = max(0, len(df) - 730)
    for i in range(start_i, len(df)):
        date = df.index[i].date()
        df_t = df.iloc[:i + 1]
        row = df_t.iloc[-1]
        common = dict(funding_8h=funding.get(date), oi_stats=None, fng=fng.get(date),
                      etf_summary=None, sopr=None, btc_d_trend=None, macro=None)
        top = compute_escape_top_score(row, df_t, **common)[0]
        low, lsig = compute_relative_low_score(row, df_t, **common)
        cyc = lsig["cycle"]["score"]
        net = compute_trend_score(row, df_t)[0]
        act = compute_composite_action(net, top, low, cyc)
        f30 = (closes[i + 30] / closes[i] - 1) * 100 if i + 30 < len(df) else None
        f60 = (closes[i + 60] / closes[i] - 1) * 100 if i + 60 < len(df) else None
        recs.append({"date": date, "close": closes[i], "top": top, "low": low, "cyc": cyc,
                     "net": net, "key": act["action_key"],
                     "label": f"{act['emoji']} {act['action']}", "f30": f30, "f60": f60})

    # 訊號轉換點
    print(f"\n{'═'*86}\n  訊號轉換點（{recs[0]['date']} ~ {recs[-1]['date']}）\n{'═'*86}")
    print(f"  {'日期':<11}{'收盤':>9}  逃頂 抄底 cyc  趨勢   訊號            其後30d   60d")
    prev = None
    for r in recs:
        if r["key"] != prev:
            f30 = "—" if r["f30"] is None else f"{r['f30']:+.0f}%"
            f60 = "—" if r["f60"] is None else f"{r['f60']:+.0f}%"
            print(f"  {str(r['date']):<11}{r['close']:>9,.0f}  {r['top']:>3} {r['low']:>4} {r['cyc']:>3} "
                  f"{r['net']:>+5}   {r['label']:<14}{f30:>7}  {f60:>6}")
            prev = r["key"]

    # 各訊號統計（前瞻報酬 = 驗證訊號方向是否對）
    print(f"\n{'═'*86}\n  各訊號統計（佔比 + 平均其後 30/60 日報酬，驗證方向）\n{'═'*86}")
    import statistics as st
    for key in dict.fromkeys(r["key"] for r in recs):   # 出現過的 action_key（保留首見順序）
        grp = [r for r in recs if r["key"] == key]
        if not grp:
            continue
        lbl = grp[0]["label"]
        f30s = [r["f30"] for r in grp if r["f30"] is not None]
        f60s = [r["f60"] for r in grp if r["f60"] is not None]
        m30 = f"{st.mean(f30s):+.1f}%" if f30s else "—"
        m60 = f"{st.mean(f60s):+.1f}%" if f60s else "—"
        print(f"  {lbl:<16} {len(grp):>3}天 ({len(grp)/len(recs)*100:>4.0f}%)  "
              f"平均其後30d {m30:>7}  60d {m60:>7}")

    # 5-6 月那波焦點
    print(f"\n{'═'*86}\n  焦點：2026-05 $82k→$59k 那波\n{'═'*86}")
    for r in recs:
        if dt.date(2026, 5, 5) <= r["date"] <= dt.date(2026, 6, 10) and r["date"].day % 3 == 0:
            print(f"  {r['date']}  ${r['close']:>7,.0f}  趨勢{r['net']:>+4} 逃頂{r['top']:>2} "
                  f"抄底{r['low']:>2}(cyc{r['cyc']})  → {r['label']}")


if __name__ == "__main__":
    main()
