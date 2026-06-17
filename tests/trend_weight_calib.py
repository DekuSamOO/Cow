"""
tests/trend_weight_calib.py
趨勢方向四維權重驗證 — 檢驗 core/trend_direction.WEIGHTS_TREND(ma40/macd30/slope15/adx15)
的權重排序是否與「各維對短中期前瞻報酬的判別力」一致。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/trend_weight_calib.py

方法：
  逐日 compute_trend_score → 取 net 與各維有號分。趨勢延續看短中期（10/20 日，
  非 60 日——P1-2 已知 60 日報酬對趨勢呈均值回歸 U 型）。
  (1) 各維有號分 → 「前瞻報酬 > 0」的單維 AUC（>0.5＝該維方向與後續報酬同向）。
     AUC 排序若與權重排序一致 → 權重方向獲支持。
  (2) 合成 net 分桶 → 前瞻報酬中位（應隨 net 遞增 = 趨勢延續）。
純驗證、不改權重（如排序明顯不符才建議微調）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from service.market_data import fetch_market_data
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators
from core.trend_direction import compute_trend_score, WEIGHTS_TREND

DIMS = ["ma_structure", "macd", "slope", "adx"]


def auc(pos, neg):
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    a = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    a.sort(key=lambda x: x[0]); r = {}; i = 0
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


def main():
    print("載入資料 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)
    close = btc["close"].values.astype(float)
    n = len(btc)

    nets, dim_scores = [], {d: [] for d in DIMS}
    fwd = {10: [], 20: []}
    valid = []
    for k in range(250, n):
        sub = btc.iloc[max(0, k - 140):k + 1]
        net, sig = compute_trend_score(btc.iloc[k], sub)
        nets.append(net)
        for d in DIMS:
            dim_scores[d].append(sig[d]["score"])
        for h in (10, 20):
            fut = close[k + 1:k + 1 + h]
            fwd[h].append(fut[-1] / close[k] - 1 if len(fut) == h else np.nan)
        valid.append(k)
    nets = np.array(nets, float)

    for h in (10, 20):
        y = np.array(fwd[h], float)
        m = ~np.isnan(y)
        print(f"\n=== 前瞻 {h} 日 ===（n={m.sum()}）")
        print("  各維有號分 → 『報酬>0』單維 AUC（排序對照權重）：")
        aucs = {}
        for d in DIMS:
            s = np.array(dim_scores[d], float)
            pos = s[m & (y > 0)]; neg = s[m & (y <= 0)]
            a = auc(pos.tolist(), neg.tolist())
            aucs[d] = a
            print(f"    {d:13s} 權重 {WEIGHTS_TREND[d]:>2}  AUC={a:.3f}")
        w_order = sorted(WEIGHTS_TREND, key=lambda d: -WEIGHTS_TREND[d])
        a_order = sorted(DIMS, key=lambda d: -(aucs[d] if aucs[d] == aucs[d] else 0))
        print(f"    權重排序 {w_order}")
        print(f"    AUC 排序 {a_order}  {'✓ 一致' if w_order == a_order else '✗ 不一致（可考慮微調）'}")
        print("  合成 net 分桶 → 前瞻報酬中位：")
        for lo, hi, lbl in [(-101, -50, "強空"), (-50, -20, "空"), (-20, 20, "中性"),
                            (20, 50, "多"), (50, 101, "強多")]:
            mm = m & (nets >= lo) & (nets < hi)
            if mm.sum() > 10:
                print(f"    {lbl:6s} n={mm.sum():4d}  報酬中位 {np.median(y[mm])*100:+5.1f}%  "
                      f"勝率 {(y[mm]>0).mean()*100:3.0f}%")

    print("\n結論：net 分桶報酬若隨趨勢單調遞增 → 趨勢分方向有效；各維 AUC 排序與權重大致一致 → 權重合理。")


if __name__ == "__main__":
    main()
