"""
scripts/tw_label_sweep.py  ·  台股維度 v0.3 弱維強化 — P4 labeling 穩健性 sweep

對 P1-P3 篩出的關鍵維/候選，掃 labeling 參數確認 AUC 排序不隨 labeling 翻轉：
  反向門檻 rev ∈ {12,15,18,25}%   ×   持有 hold ∈ {30,60,90} 日

用 raw 方向 AUC（一律 higher_means_positive=True）同時報兩側：
  raw_high = P(真頂 swing-high 的維值 > 假頂)   → 高值→真頂 的強度
  raw_low  = P(真底 swing-low  的維值 > 假底)   → 高值→真底 的強度

判讀：
  方向性逃頂訊號  = raw_high 穩定 >0.55、raw_low ≈0.5（不對稱）→ 真訊號
  移動幅度混淆    = raw_high 與 raw_low 同時 >0.55（雙向）→ 只是「會大動」代理，非逃頂
  複驗估值絕對>分位 = PE/PB 絕對 vs 分位的 raw_high 比較

複用 tw_variant_backtest.build_variants（變體只建一次）+ tw_dim_backtest.auc/per_stock_pctile。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_dim_backtest import auc, per_stock_pctile          # noqa: E402
from tw_variant_backtest import build_variants, _PANEL, _SPLIT, _W  # noqa: E402

_REVS = (0.12, 0.15, 0.18, 0.25)
_HOLDS = (30, 60, 90)

# (顯示名, 欄位)；raw 方向統一「高值→真頂?」「高值→真底?」
_DIMS = [
    ("估值PE絕對",   "PE"),
    ("估值PE分位",   "PE_pctile"),
    ("估值PB絕對",   "PB"),
    ("估值PB分位",   "PB_pctile"),
    ("融資變化%",    "fin_chg_pct"),
    ("成交量分位",   "vol_pctile"),
    ("爆量倍數",     "vol_ratio"),
    ("券資比",       "short_margin"),
    ("波動率分位",   "volat_pctile"),
]


def main():
    df = pd.read_parquet(_PANEL)
    df["Date"] = pd.to_datetime(df["Date"])
    print("[P4] 建變體特徵（一次）…")
    df = build_variants(df)
    # 估值分位（複驗絕對>分位）
    df["PE_pctile"] = per_stock_pctile(df, "PE")
    df["PB_pctile"] = per_stock_pctile(df, "PB")

    df = df.sort_values(["Stock_ID", "Date"])
    g = df.groupby("Stock_ID", sort=False)["price"]
    # 各持有期 forward 報酬（一次算齊）
    for h in _HOLDS:
        df[f"fwd{h}"] = g.transform(lambda s, h=h: s.shift(-h) / s - 1)
    # swing 偵測（W 固定，與 baseline 同）
    win = 2 * _W + 1
    roll_min = g.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).min())
    roll_max = g.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).max())
    df["is_swing_low"] = df["price"] <= roll_min
    df["is_swing_high"] = df["price"] >= roll_max

    test = df[df["Date"] >= _SPLIT].copy()
    configs = [(rev, h) for h in _HOLDS for rev in _REVS]
    hdr = "  ".join(f"{int(r*100)}%/{h}d" for r, h in configs)

    def sweep(side):  # side: 'high'(逃頂) or 'low'(抄底)
        is_swing = "is_swing_high" if side == "high" else "is_swing_low"
        print(f"\n══ {'逃頂 raw_high（高值→真頂強度）' if side=='high' else '抄底 raw_low（高值→真底強度）'} ══")
        print(f"  {'維度':<12}  {hdr}")
        for name, col in _DIMS:
            cells = []
            for rev, h in configs:
                fr = test[test[is_swing] & test[f"fwd{h}"].notna()]
                real = (fr[f"fwd{h}"] <= -rev) if side == "high" else (fr[f"fwd{h}"] >= rev)
                a, n1, n0 = auc(fr[col], real, True)   # raw：高值→真事件
                cells.append("  —  " if a is None else f"{a:.3f}")
            print(f"  {name:<12}  " + "  ".join(f"{c:>7}" for c in cells))

    sweep("high")
    sweep("low")
    print("\n判讀：方向性逃頂訊號=逃頂列穩>0.55且抄底列≈0.5；移動幅度混淆=兩側同時>0.55。"
          "\n      18%/60d 對應 baseline 設定。成交量分位若逃頂穩>0.55、抄底穩≈0.5 即過關。")


if __name__ == "__main__":
    main()
