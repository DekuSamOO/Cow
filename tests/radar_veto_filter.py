"""
tests/radar_veto_filter.py
路線 A：把雷達從「進場訊號」降級為「否決濾網」，並量化它值不值得（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/radar_veto_filter.py
  --refresh   忽略快取重跑回放（回放很慢，預設吃 scratchpad 快取）

動機：`Work\\雷達修復_TASKS.md` T1~T4 的結論是四個雷達裡三個沒有「進場級」訊號。
但「沒有好到能叫你進場」不等於「沒有好到能叫你別進場」——**否決條件的門檻比進場條件低**：
進場要求 lift 高且 recall 夠；否決只要求「被否決的日子確實比較差」，
即使它只涵蓋一小部分日子，剔掉它們仍是淨賺。

判準（三條，全部要過才算「這個濾網值得用」）：
  V1 被否決日的其後報酬**中位數**顯著低於未否決日（Mann-Whitney p<0.05）
  V2 被否決日的**下檔**（P25 報酬）也較差 —— 排除「只是波動大」而非「真的比較差」
  V3 否決比例落在 **2%~40%**：太低＝幾乎不作用；太高＝等於長期空手，
     那不是濾網是離場決策，要用另一套標準評估

⚠️ 這是**全樣本 in-sample 的效果量測**，不是新假說的驗收。
   T1 的 holdout 驗收次數已用掉；本檔只回答「若照現有分數當否決條件，歷史上會怎樣」，
   **不得據此宣稱未來績效**。要宣稱，需要一段尚未動用的資料。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

CACHE = os.environ.get(
    "RADAR_CACHE",
    r"D:/Users/63191/AppData/Local/Temp/claude/D--Users-63191-Desktop-Obsidian-Zettelkasten"
    r"/b014aaaa-98cb-4998-9919-6110fb50236c/scratchpad")
FWD = 180          # 加密用曆日；台股/美股用交易日 120（約半年），見各自呼叫處


def fwd_ret(close: np.ndarray, h: int) -> np.ndarray:
    n = len(close)
    o = np.full(n, np.nan)
    for i in range(n - h):
        o[i] = close[i + h] / close[i] - 1
    return o


def veto_eval(name, score: np.ndarray, fwd: np.ndarray, thresholds,
              veto_when_high: bool):
    """對一組門檻評估否決濾網。veto_when_high=True → 分數>=門檻即否決（逃頂用）。"""
    ok = np.isfinite(score) & np.isfinite(fwd)
    s, f = score[ok], fwd[ok]
    base_med = float(np.median(f))
    print("  %s　n=%d｜全體其後報酬中位 %+.1f%%｜P25 %+.1f%%"
          % (name, len(f), base_med * 100, np.percentile(f, 25) * 100))
    print("  %-8s %-11s %-13s %-13s %-13s %-9s %s"
          % ("門檻", "否決比例", "被否決中位", "未否決中位", "被否決P25", "p 值", "判定"))
    rows = []
    for t in thresholds:
        v = (s >= t) if veto_when_high else (s <= t)
        if v.sum() < 30 or (~v).sum() < 30:
            print("  %-8d %-11s 樣本不足" % (t, "%.1f%%" % (v.mean() * 100)))
            continue
        m_v, m_k = float(np.median(f[v])), float(np.median(f[~v]))
        p25_v, p25_k = float(np.percentile(f[v], 25)), float(np.percentile(f[~v], 25))
        try:
            _, p = mannwhitneyu(f[v], f[~v], alternative="less")
        except ValueError:
            p = np.nan
        v1 = np.isfinite(p) and p < 0.05
        v2 = p25_v < p25_k
        v3 = 0.02 <= v.mean() <= 0.40
        verdict = "✅ 可用" if (v1 and v2 and v3) else "❌ " + " ".join(
            x for x, okk in (("V1", v1), ("V2", v2), ("V3", v3)) if not okk) + " 未過"
        print("  %-8d %-11s %-13s %-13s %-13s %-9s %s"
              % (t, "%.1f%%" % (v.mean() * 100), "%+.1f%%" % (m_v * 100),
                 "%+.1f%%" % (m_k * 100), "%+.1f%%" % (p25_v * 100),
                 "—" if not np.isfinite(p) else "%.1e" % p, verdict))
        rows.append((t, v.mean(), m_v, m_k, p, v1 and v2 and v3))
    return rows


# ══════════════════════════════════════════════════════════════════════════
def crypto_scores(refresh=False):
    path = os.path.join(CACHE, "veto_crypto.csv")
    if os.path.exists(path) and not refresh:
        d = pd.read_csv(path, index_col=0, parse_dates=True)
        return d
    from tests.radar_decision_bench import load_crypto, replay_crypto
    btc, fund, mvrv, sopr, etf, fng = load_crypto()
    sc = replay_crypto(btc, fund, mvrv, sopr, etf, fng)
    c = btc["close"] if "close" in btc.columns else btc["Close"]
    sc["close"] = c.reindex(sc.index)
    sc.to_csv(path)
    return sc


def tw_scores(refresh=False):
    path = os.path.join(CACHE, "veto_tw.csv")
    if os.path.exists(path) and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    import sqlite3
    from tests.radar_bench_tw import TW_DB, DEFAULT_STOCKS, load_stock, replay
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)
    frames = []
    for sid in DEFAULT_STOCKS:
        loaded = load_stock(con, sid)
        if loaded is None:
            continue
        df, fin_chg, inst, pe, pb, tdcc = loaded
        sc = replay(df, fin_chg, inst, pe, pb, tdcc)
        sc["close"] = df["close"].reindex(sc.index)
        sc["sid"] = sid
        frames.append(sc)
        print("    %s ok" % sid)
    con.close()
    out = pd.concat(frames)
    out.to_csv(path)
    return out


def us_scores(refresh=False):
    path = os.path.join(CACHE, "veto_us.csv")
    if os.path.exists(path) and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    from tests.radar_decision_bench import load_us_prices, replay_us
    frames = []
    for t, df in load_us_prices(["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN",
                                 "GOOGL", "META"]).items():
        sc = replay_us(df)
        sc["close"] = df["close"].reindex(sc.index)
        sc["sid"] = t
        frames.append(sc)
        print("    %s ok" % t)
    out = pd.concat(frames)
    out.to_csv(path)
    return out


def per_symbol_fwd(d: pd.DataFrame, h: int) -> np.ndarray:
    """多標的合併時，前瞻報酬要**逐標的**算，不能跨標的接續。"""
    out = []
    for _, g in d.groupby("sid", sort=False):
        out.append(fwd_ret(g["close"].values, h))
    return np.concatenate(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    print("=" * 96)
    print("路線 A：雷達當「否決濾網」的效果量測（全樣本 in-sample，非驗收）")
    print("判準 V1 被否決日報酬顯著較差(p<0.05)｜V2 下檔 P25 也較差｜V3 否決比例 2%~40%")
    print("=" * 96)

    print()
    print("### 加密 BTC（其後 180 曆日）")
    c = crypto_scores(args.refresh)
    f = fwd_ret(c["close"].values, FWD)
    veto_eval("逃頂高分＝不加碼", c["top"].values.astype(float), f,
              [25, 35, 45, 49], veto_when_high=True)
    print()
    veto_eval("抄底低分＝不進場", c["low"].values.astype(float), f,
              [5, 10, 15, 20, 25], veto_when_high=False)

    print()
    print("### 台股 14 檔（其後 120 交易日）")
    t = tw_scores(args.refresh)
    ft = per_symbol_fwd(t, 120)
    veto_eval("逃頂高分＝減碼/不加碼", t["top"].values.astype(float), ft,
              [45, 55, 65, 75], veto_when_high=True)
    print()
    veto_eval("抄底低分＝不進場", t["low"].values.astype(float), ft,
              [10, 15, 20, 30], veto_when_high=False)

    print()
    print("### 美股 8 檔（其後 120 交易日）")
    u = us_scores(args.refresh)
    fu = per_symbol_fwd(u, 120)
    veto_eval("逃頂高分＝減碼/不加碼", u["top"].values.astype(float), fu,
              [30, 45, 55, 65], veto_when_high=True)
    print()
    veto_eval("抄底低分＝不進場", u["low"].values.astype(float), fu,
              [10, 15, 20, 30], veto_when_high=False)


if __name__ == "__main__":
    main()
