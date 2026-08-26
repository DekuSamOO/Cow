"""
tests/cycle_dim_universal.py
路線 B：純價格「週期/估值錨」維度，在**從未測過的市場**上驗證（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/cycle_dim_universal.py

為什麼要在別的市場測：加密逃頂的 cycle 維假說（`tests/top_cycle_dim_calib.py`）
**holdout 驗收次數已用掉**，同一假說不得換參數在同一段資料重測。
但「價格相對自身長期錨的位置能預示頂部」是一個**跨市場命題**——
台股與美股的逃頂側從來沒有測過任何 cycle 維（現行台股七維、美股三維都沒有），
所以那兩個市場是這個命題乾淨的 out-of-sample。

候選錨（全部純價格、無需新資料源）：
  dist_ath   距歷史最高（expanding cummax）           ← 全市場皆可算
  r_sma1400  價/200週均（約 5.5 年）                  ← 需長歷史，台股/美股 2021 後才有值
  r_sma730   價/2年均（Mayer 式）                     ← **對照組**：BTC 上實測 r=+0.138 方向相反，
                                                        若在台股/美股也是正的，代表「短錨＝動能」可複製
指標一律轉 PiT expanding 分位（不看未來），再比對其後報酬（體制量法）。

判準（跨市場一致性，比單一市場的 p 值更重要）：
  B1 該錨在**台股與美股**的跨標的中位 Spearman r 皆為負（期望方向）
  B2 方向正確的標的比例 >= 70%（14 檔台股取 >=10、8 檔美股取 >=6）
  B3 效果量級與 BTC 同量級（BTC 冪律 -0.45／200週 -0.42）——至少 |r| >= 0.10
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from tests.radar_bench_tw import TW_DB, DEFAULT_STOCKS
from tests.radar_eval_standard import realized_sigma_h, swing_events, base_rate, threshold_metrics

FWD_TD = 120        # 交易日（約半年）
MIN_OBS = 400       # expanding 分位的最小樣本


def anchors(close: pd.Series) -> pd.DataFrame:
    d = pd.DataFrame(index=close.index)
    d["dist_ath"] = close / close.cummax() - 1
    d["r_sma1400"] = close / close.rolling(1400).mean()
    d["r_sma730"] = close / close.rolling(730).mean()
    return d


def pit_rank(s: pd.Series) -> pd.Series:
    return s.expanding(MIN_OBS).rank(pct=True) * 100


def fwd(c: np.ndarray, h: int) -> np.ndarray:
    n = len(c); o = np.full(n, np.nan)
    for i in range(n - h):
        o[i] = c[i + h] / c[i] - 1
    return o


def load_tw_prices():
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)
    out = {}
    for sid in DEFAULT_STOCKS:
        q = pd.read_sql("select Date, Close from daily_quotes where Stock_ID=? order by Date",
                        con, params=(sid,), parse_dates=["Date"])
        if len(q) < MIN_OBS + FWD_TD:
            continue
        out[sid] = q.set_index("Date")["Close"].dropna()
    con.close()
    return out


def load_us_prices_only():
    from service.ohlc_universal import fetch_ohlc
    out = {}
    for t in ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "META"]:
        try:
            out[t] = fetch_ohlc(t, rng="10y")["close"].dropna()
        except Exception as e:
            print("  %s 抓取失敗（%s）→ 跳過" % (t, str(e)[:50]))
    return out


def survey(market, prices: dict):
    print()
    print("### %s（%d 檔，其後 %d 交易日）" % (market, len(prices), FWD_TD))
    print("  %-8s %-22s %-22s %s" % ("標的", "距ATH r", "價/200週均 r", "價/2年均 r（對照）"))
    agg = {"dist_ath": [], "r_sma1400": [], "r_sma730": []}
    for sid, c in prices.items():
        a = anchors(c)
        f = fwd(c.values, FWD_TD)
        row = []
        for k in ("dist_ath", "r_sma1400", "r_sma730"):
            p = pit_rank(a[k]).values
            ok = np.isfinite(p) & np.isfinite(f)
            if ok.sum() < 200:
                row.append("n<200"); continue
            r, pv = spearmanr(p[ok], f[ok])
            agg[k].append(r)
            row.append("%+.3f (p=%.0e)" % (r, pv))
        print("  %-8s %-22s %-22s %s" % (sid, *row))
    print("  %-8s %-22s %-22s %s" % ("中位",
                                     "%+.3f" % np.median(agg["dist_ath"]) if agg["dist_ath"] else "—",
                                     "%+.3f" % np.median(agg["r_sma1400"]) if agg["r_sma1400"] else "—",
                                     "%+.3f" % np.median(agg["r_sma730"]) if agg["r_sma730"] else "—"))
    for k, lab in (("dist_ath", "距ATH"), ("r_sma1400", "價/200週均"), ("r_sma730", "價/2年均")):
        v = agg[k]
        if not v:
            continue
        print("    %-12s 方向正確（r<0） %d/%d｜中位 %+.3f｜全距 %+.3f ~ %+.3f"
              % (lab, sum(1 for x in v if x < 0), len(v), np.median(v), min(v), max(v)))
    return agg


def main():
    print("=" * 96)
    print("路線 B：純價格週期錨在**未測過的市場**上驗證（台股／美股逃頂側）")
    print("判準 B1 兩市場中位 r 皆負｜B2 方向正確 >=70%｜B3 |中位 r| >= 0.10")
    print("=" * 96)
    tw = survey("台股", load_tw_prices())
    us = survey("美股", load_us_prices_only())

    print()
    print("=" * 96)
    print("判準檢核")
    print("=" * 96)
    print("  %-14s %-12s %-12s %-14s %-14s %s"
          % ("錨", "台股中位 r", "美股中位 r", "台股方向正確", "美股方向正確", "判定"))
    for k, lab in (("dist_ath", "距ATH"), ("r_sma1400", "價/200週均"), ("r_sma730", "價/2年均")):
        a, b = tw.get(k, []), us.get(k, [])
        if not a or not b:
            continue
        ma, mb = float(np.median(a)), float(np.median(b))
        ca, cb = sum(1 for x in a if x < 0), sum(1 for x in b if x < 0)
        b1 = ma < 0 and mb < 0
        b2 = (ca / len(a) >= 0.70) and (cb / len(b) >= 0.70)
        b3 = abs(ma) >= 0.10 and abs(mb) >= 0.10
        verdict = "✅ 三條全過" if (b1 and b2 and b3) else "❌ " + " ".join(
            x for x, ok in (("B1", b1), ("B2", b2), ("B3", b3)) if not ok) + " 未過"
        print("  %-14s %-12s %-12s %-14s %-14s %s"
              % (lab, "%+.3f" % ma, "%+.3f" % mb, "%d/%d" % (ca, len(a)),
                 "%d/%d" % (cb, len(b)), verdict))
    print()
    print("對照：BTC 上的同一組指標（`tests/top_cycle_dim_calib.py` 全期 expanding 分位）")
    print("  冪律比 -0.456｜價/200週均 -0.422｜距ATH -0.223｜Mayer(價/2年均) **+0.138 方向相反**")


if __name__ == "__main__":
    main()
