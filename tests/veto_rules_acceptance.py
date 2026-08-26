"""
tests/veto_rules_acceptance.py
兩條否決規則的**獨立驗收**（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/veto_rules_acceptance.py

背景：`Github\\Cow\\歷程\\20260826findings_否決濾網與週期錨.md` 得到兩條規則，
但兩條都是**全樣本 in-sample**、門檻還是看著結果挑的。上面板前必須用沒看過的資料驗一次。

驗收資料的獨立性設計（不用時間切分，因為 BTC 的時間 holdout 在 T1 已被 cycle 維假說用掉）：
  R1 加密抄底低分否決 → 改測**非 BTC 幣對**（ETH/SOL/BNB/XRP）。
     這是生產上真的會跑的路徑（`BitcoinMonitor(is_btc=False)`，cap 72），
     資料源、標的、分數組成都與設計時的 BTCUSDT 不同。
  R2 週期錨門檻      → 換**全新標的池**（台股 15 檔、美股 8 檔，與設計池零重疊），
     門檻沿用設計時訂的（台股 >=70、美股 >=90 分位），**不重新挑**。

⚠️ 判準寫死在本檔，跑之前就定案，事後不得調整。任一條沒過即記錄否決、不上面板。
⚠️ 逐檔評估（拿每檔跟自己比）—— 池化會產生 Simpson's paradox，設計階段已踩過一次。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

# ── 驗收判準（**定死**）────────────────────────────────────────────────────
ACCEPT = """
R1 加密抄底 <=5 分 → 不進場（測 ETH/SOL/BNB/XRP，皆未用於設計）
   R1a >=3/4 檔的「被否決日其後 180 日報酬中位」低於該檔未否決日
   R1b 跨檔的中位報酬差為負
   R1c 各檔否決比例的中位落在 2%~40%

R2 價/200週均分位 → 不加碼（台股 >=70、美股 >=90；全新標的池，門檻不重挑）
   R2a 台股新池 >=70% 的可評標的方向正確（被否決日報酬較差）
   R2b 美股新池 >=70% 的可評標的方向正確
   R2c 兩池的跨檔中位報酬差皆為負
"""

# 與設計池零重疊
TW_NEW = ["2308", "2881", "2882", "1216", "2207", "2379", "3711", "2357",
          "2395", "4938", "1303", "2105", "9910", "2801", "2409"]
US_NEW = ["JPM", "JNJ", "WMT", "XOM", "PG", "KO", "DIS", "INTC"]
CRYPTO_NEW = ["ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD"]

MIN_OBS = 400
TW_GATE, US_GATE = 70, 90
LOW_VETO = 5


def fwd(c: np.ndarray, h: int) -> np.ndarray:
    n = len(c); o = np.full(n, np.nan)
    for i in range(n - h):
        o[i] = c[i + h] / c[i] - 1
    return o


def veto_stats(score: np.ndarray, f: np.ndarray, thr: float, high_is_veto: bool):
    ok = np.isfinite(score) & np.isfinite(f)
    s, ff = score[ok], f[ok]
    v = (s >= thr) if high_is_veto else (s <= thr)
    if v.sum() < 30 or (~v).sum() < 30:
        return None
    return float(v.mean()), float(np.median(ff[v]) - np.median(ff[~v]))


# ══════════════════════════════════════════════════════════════════════════
# R1：非 BTC 幣對的抄底分數
# ══════════════════════════════════════════════════════════════════════════
def r1():
    from core.indicators import calculate_technical_indicators
    from core.bear_bottom import calculate_bear_bottom_indicators
    from core.relative_low import compute_relative_low_score
    from service.ohlc_universal import fetch_ohlc
    from service.realtime import fetch_fng_history
    try:
        from service.funding_history import funding_ann_hist  # noqa: F401
        have_funding = True
    except Exception:
        have_funding = False
    fng = fetch_fng_history() or {}
    print()
    print("### R1　加密抄底 <=%d 分 → 不進場（非 BTC 幣對）" % LOW_VETO)
    print("  資料：Yahoo v8 日線 + F&G 全史；MVRV/SOPR/ETF 為 BTC 專屬 → None")
    print("  （這正是生產上非 BTC 幣對的實際輸入組成，cap 72）")
    print("  %-10s %-8s %-11s %-13s %s" % ("標的", "n", "否決比例", "報酬差(否決-未)", "方向"))
    ratios, diffs = [], []
    for sym in CRYPTO_NEW:
        try:
            df = calculate_bear_bottom_indicators(
                calculate_technical_indicators(fetch_ohlc(sym, rng="10y")))
        except Exception as e:
            print("  %-10s 抓取失敗（%s）→ 跳過" % (sym, str(e)[:40]))
            continue
        if df is None or len(df) < 600:
            print("  %-10s 資料不足 → 跳過" % sym)
            continue
        scores = []
        for i in range(300, len(df)):
            key = df.index[i].strftime("%Y-%m-%d")
            scores.append(compute_relative_low_score(
                df.iloc[i], df.iloc[max(0, i - 140):i + 1],
                funding_8h=None, oi_stats=None, etf_summary=None, sopr=None,
                fng=fng.get(key), btc_d_trend=None, macro=None, mvrv_z=None,
                rsi_pct_enabled=False)[0])
        s = np.array(scores, float)
        c = df["close"].values[300:]
        f = fwd(c, 180)
        st = veto_stats(s, f, LOW_VETO, high_is_veto=False)
        if st is None:
            print("  %-10s 兩側樣本 <30 → 不評" % sym)
            continue
        ratio, diff = st
        ratios.append(ratio); diffs.append(diff)
        print("  %-10s %-8d %-11s %-13s %s"
              % (sym, len(s), "%.1f%%" % (ratio * 100), "%+.1f%%" % (diff * 100),
                 "✅ 較差" if diff < 0 else "❌ 反而較好"))
    if not diffs:
        return None
    worse = sum(1 for d in diffs if d < 0)
    print("  → 方向正確 %d/%d｜跨檔中位報酬差 %+.1f%%｜否決比例中位 %.1f%%"
          % (worse, len(diffs), np.median(diffs) * 100, np.median(ratios) * 100))
    return {"n": len(diffs), "worse": worse, "med_diff": float(np.median(diffs)),
            "med_ratio": float(np.median(ratios))}


# ══════════════════════════════════════════════════════════════════════════
# R2：週期錨在全新標的池
# ══════════════════════════════════════════════════════════════════════════
def anchor_pct(close: pd.Series) -> pd.Series:
    r = close / close.rolling(1400).mean()
    return r.expanding(MIN_OBS).rank(pct=True) * 100


def r2_market(label, prices: dict, gate: int, h: int):
    print()
    print("### R2　%s：價/200週均分位 >= %d → 不加碼（門檻沿用設計值，未重挑）" % (label, gate))
    print("  %-10s %-8s %-11s %-13s %s" % ("標的", "可評n", "否決比例", "報酬差(否決-未)", "方向"))
    diffs, ratios = [], []
    for sid, c in prices.items():
        p = anchor_pct(c).values
        f = fwd(c.values, h)
        st = veto_stats(p, f, gate, high_is_veto=True)
        if st is None:
            print("  %-10s 兩側樣本 <30 → 不評（200週均需 5.5 年暖機）" % sid)
            continue
        ratio, diff = st
        ratios.append(ratio); diffs.append(diff)
        print("  %-10s %-8d %-11s %-13s %s"
              % (sid, int(np.isfinite(p).sum()), "%.1f%%" % (ratio * 100),
                 "%+.1f%%" % (diff * 100), "✅ 較差" if diff < 0 else "❌ 反而較好"))
    if not diffs:
        return None
    worse = sum(1 for d in diffs if d < 0)
    print("  → 方向正確 %d/%d（%.0f%%）｜跨檔中位報酬差 %+.1f%%"
          % (worse, len(diffs), worse / len(diffs) * 100, np.median(diffs) * 100))
    return {"n": len(diffs), "worse": worse, "med_diff": float(np.median(diffs))}


def load_tw_new():
    from tests.radar_bench_tw import TW_DB
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)
    out = {}
    for sid in TW_NEW:
        q = pd.read_sql("select Date, Close from daily_quotes where Stock_ID=? order by Date",
                        con, params=(sid,), parse_dates=["Date"])
        if len(q) < 1800:
            continue
        out[sid] = q.set_index("Date")["Close"].dropna()
    con.close()
    return out


def load_us_new():
    from service.ohlc_universal import fetch_ohlc
    out = {}
    for t in US_NEW:
        try:
            out[t] = fetch_ohlc(t, rng="10y")["close"].dropna()
        except Exception as e:
            print("  %s 抓取失敗（%s）→ 跳過" % (t, str(e)[:40]))
    return out


def main():
    print("=" * 92)
    print("兩條否決規則的獨立驗收　·　判準已定死，事後不得調整")
    print("=" * 92)
    print(ACCEPT)

    a = r1()
    tw = r2_market("台股新池（15 檔，與設計池零重疊）", load_tw_new(), TW_GATE, 120)
    us = r2_market("美股新池（8 檔，與設計池零重疊）", load_us_new(), US_GATE, 120)

    print()
    print("=" * 92)
    print("判準檢核")
    print("=" * 92)
    if a:
        r1a = a["worse"] >= 3 and a["n"] >= 4 or (a["worse"] / a["n"] >= 0.75)
        r1b = a["med_diff"] < 0
        r1c = 0.02 <= a["med_ratio"] <= 0.40
        print("  R1a 方向正確 %d/%d → %s" % (a["worse"], a["n"], "✅" if r1a else "❌"))
        print("  R1b 跨檔中位報酬差 %+.1f%% → %s" % (a["med_diff"] * 100, "✅" if r1b else "❌"))
        print("  R1c 否決比例中位 %.1f%% → %s" % (a["med_ratio"] * 100, "✅" if r1c else "❌"))
        print("  R1 判定：%s" % ("✅ 通過，可上面板" if (r1a and r1b and r1c) else "❌ 否決"))
    else:
        print("  R1 無可評標的 → 無法驗收（不得視為通過）")
    for tag, res, lab in (("R2a", tw, "台股新池"), ("R2b", us, "美股新池")):
        if res:
            ok = res["worse"] / res["n"] >= 0.70
            print("  %s %s 方向正確 %d/%d（%.0f%%）→ %s"
                  % (tag, lab, res["worse"], res["n"], res["worse"] / res["n"] * 100,
                     "✅" if ok else "❌"))
        else:
            print("  %s %s 無可評標的 → 無法驗收" % (tag, lab))
    if tw and us:
        r2c = tw["med_diff"] < 0 and us["med_diff"] < 0
        print("  R2c 兩池中位報酬差 台股 %+.1f%%／美股 %+.1f%% → %s"
              % (tw["med_diff"] * 100, us["med_diff"] * 100, "✅" if r2c else "❌"))
        ok2 = (tw["worse"] / tw["n"] >= 0.70) and (us["worse"] / us["n"] >= 0.70) and r2c
        print("  R2 判定：%s" % ("✅ 通過，可上面板" if ok2 else "❌ 否決"))


if __name__ == "__main__":
    main()
