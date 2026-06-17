"""
tests/funding_threshold_calib.py
資金費率門檻校準 — 用幣安 BTC 資費史「回歸」重訂逃頂(正費率)/抄底(負費率)的給分門檻。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/funding_threshold_calib.py

動機（2026-06）：
  舊門檻把大部分分數鎖在年化 ≥70~90% 的尾巴。實測幣安資費史（2020-12+，~2000 日）顯示：
    - 年化 ≥90% 僅 35 日(1.76%)、≥70% 55 日，幾乎全在 2021 狂熱；且回撤未比 50% 更深。
    - 後 60 日最大回撤在年化 ≥30% 由 ~-10% 翻倍至 ~-18%（轉折），≥50% 飽和(~-19%)，≥70% 未更深。
    - 負費率極稀有：≤-15/-20/-30% 僅 8/4/3 日(危機)；判別力在「淺負」(Youden 最佳 ≤-3%)。
  → 門檻前移、對齊回撤/反彈轉折，而非鎖在罕見極值。

方法：
  資料 = 日線收盤 + 幣安資費日均(年化)。標頂(swing high+其後60日回撤≥18%)/底(swing low+反彈≥18%)。
  對「年化資費」做：(1)分布分位 (2)各桶→其後60日最大回撤/反彈 (3)頂/底條件單維 AUC + Youden 門檻。
  AUC 以 Mann-Whitney U（與其餘 backtest 同實作）。

校準結論（拍板門檻，已落地 core/relative_high._score_derivatives /
                              core/relative_low._score_derivatives_low）：
  逃頂正費率(max 20)：≥50→20 | ≥40→17 | ≥30→14 | ≥20→6 | ≥12→2 | <12→0
  抄底負費率(max 10)：≤-20→10 | ≤-10→8 | ≤-5→6 | ≤-2→3 | <0→1 | ≥0→0
  資料長大後重跑本腳本，據新分布/轉折微調。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from service.market_data import fetch_market_data
from service.onchain import fetch_aux_history

H = 60            # 前瞻視窗（日）
ORDER = 10        # swing 視窗
MOVE = 0.18       # 頂/底標記的其後反向幅度門檻


def auc(pos, neg):
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    a = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    a.sort(key=lambda x: x[0])
    r = {}; i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[j + 1][0] == a[i][0]:
            j += 1
        rr = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[k] = rr
        i = j + 1
    rs = sum(r[k] for k, (v, l) in enumerate(a) if l == 1)
    return (rs - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _load():
    btc, _ = fetch_market_data()
    btc.index = pd.to_datetime(btc.index)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)
    _, _, fund = fetch_aux_history()
    f = fund.copy()
    if f.index.tz is not None:
        f.index = f.index.tz_localize(None)
    ann = (f["fundingRate"].dropna().resample("D").mean() * 3 * 365).dropna()   # 年化 %
    close = btc["close"].reindex(ann.index, method="nearest").values
    high = btc["high"].reindex(ann.index, method="nearest").values
    low = btc["low"].reindex(ann.index, method="nearest").values
    return ann, close, high, low


def _swings(series_vals, close, is_top):
    """頂(is_top=True)：swing high + 其後60日回撤≥MOVE；底：swing low + 其後60日反彈≥MOVE。"""
    n = len(series_vals)
    out = []
    for i in range(ORDER, n - ORDER):
        w = series_vals[i - ORDER:i + ORDER + 1]
        ext = (series_vals[i] == np.nanmax(w)) if is_top else (series_vals[i] == np.nanmin(w))
        if not ext:
            continue
        fut = close[i + 1:i + H + 1]
        if not len(fut):
            continue
        move = (fut.min() / close[i] - 1) if is_top else (fut.max() / close[i] - 1)
        if (move <= -MOVE) if is_top else (move >= MOVE):
            out.append(i)
    return out


def main():
    print("載入資料 …")
    ann, close, high, low = _load()
    av = ann.values
    n = len(ann)
    print(f"資費史 {ann.index.min().date()} ~ {ann.index.max().date()}  n={n} 日")

    # ── 正費率 / 逃頂 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 64 + "\n正費率 → 逃頂（年化資費分布 / 回撤 / 頂部 AUC）\n" + "=" * 64)
    print("分位:", {f"p{q}": round(np.percentile(av, q), 1) for q in [50, 90, 95, 99, 100]})
    print(f"≥30/50/70/90% 天數佔比: "
          + " ".join(f"{t}%={(av>=t).mean()*100:.1f}%" for t in (30, 50, 70, 90)))
    fwd_dd = np.array([(np.nanmin(close[i+1:i+H+1]) / close[i] - 1) * 100 if i+1 < n else np.nan
                       for i in range(n)])
    print("各正費率桶 → 後60日最大回撤中位:")
    for lo, hi in [(-1e9, 0), (0, 15), (15, 30), (30, 50), (50, 70), (70, 1e9)]:
        m = (av >= lo) & (av < hi) & ~np.isnan(fwd_dd)
        if m.sum() > 3:
            print(f"  [{lo:>5.0f},{hi:>4.0f})%: n={m.sum():4d}  回撤中位 {np.median(fwd_dd[m]):6.1f}%")
    tops = _swings(high, close, is_top=True)
    rng = np.random.default_rng(0)
    nontop = [k for k in range(ORDER, n - ORDER) if all(abs(k - t) > 30 for t in tops)]
    nt = list(rng.choice(nontop, size=min(len(tops) * 3, len(nontop)), replace=False))
    print(f"\n頂部 {len(tops)} / 非頂 {len(nt)}  單維 AUC={auc([av[i] for i in tops], [av[i] for i in nt]):.3f}")
    _youden([av[i] for i in tops], [av[i] for i in nt], [15, 20, 30, 40, 50, 70, 90], ge=True)

    # ── 負費率 / 抄底 ───────────────────────────────────────────────────────────
    print("\n" + "=" * 64 + "\n負費率 → 抄底（負費率天數 / 反彈 / 底部 AUC）\n" + "=" * 64)
    print(f"負費率天數 {(av<0).sum()} ({(av<0).mean()*100:.1f}%)；最深 {av.min():.1f}%")
    print(f"≤-5/-15/-20/-30% 天數: "
          + " ".join(f"{t}%={(av<=t).sum()}日" for t in (-5, -15, -20, -30)))
    fwd_up = np.array([(np.nanmax(close[i+1:i+H+1]) / close[i] - 1) * 100 if i+1 < n else np.nan
                       for i in range(n)])
    print("各負費率桶 → 後60日最大反彈中位:")
    for lo, hi in [(-1e9, -30), (-30, -20), (-20, -15), (-15, -5), (-5, 0), (0, 1e9)]:
        m = (av >= lo) & (av < hi) & ~np.isnan(fwd_up)
        if m.sum() > 3:
            print(f"  [{lo:>5.0f},{hi:>4.0f})%: n={m.sum():4d}  反彈中位 {np.median(fwd_up[m]):6.1f}%")
    bottoms = _swings(low, close, is_top=False)
    nonb = [k for k in range(ORDER, n - ORDER) if all(abs(k - t) > 30 for t in bottoms)]
    nb = list(rng.choice(nonb, size=min(len(bottoms) * 3, len(nonb)), replace=False))
    # 負費率越深越像底 → 用 −年化資費當分數
    print(f"\n底部 {len(bottoms)} / 非底 {len(nb)}  "
          f"單維 AUC(負費率→底)={auc([-av[i] for i in bottoms], [-av[i] for i in nb]):.3f}")
    _youden([av[i] for i in bottoms], [av[i] for i in nb], [-3, -5, -8, -10, -15, -20], ge=False)

    print("\n拍板門檻見檔頭 docstring；已落地 core/relative_high & core/relative_low。")


def _youden(pos, neg, thresholds, ge=True):
    """各門檻 命中/誤報/Youden's J；ge=True 為 ≥thr（逃頂），False 為 ≤thr（抄底）。"""
    print("各門檻 命中率 / 誤報率 / Youden's J:")
    best = None
    for thr in thresholds:
        hit = np.mean([(v >= thr) if ge else (v <= thr) for v in pos])
        fp = np.mean([(v >= thr) if ge else (v <= thr) for v in neg])
        J = hit - fp
        if best is None or J > best[1]:
            best = (thr, J, hit, fp)
        sign = "≥" if ge else "≤"
        print(f"  {sign}{thr:>4}%: 命中 {hit*100:4.0f}%  誤報 {fp*100:4.0f}%  J={J:+.2f}")
    sign = "≥" if ge else "≤"
    print(f"  → Youden 最佳 {sign}{best[0]}%（命中 {best[2]*100:.0f}% / 誤報 {best[3]*100:.0f}%）")


if __name__ == "__main__":
    main()
