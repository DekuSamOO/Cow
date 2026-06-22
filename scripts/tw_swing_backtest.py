"""
scripts/tw_swing_backtest.py  ·  台股維度校準 — S2b swing-only 重測

S2 用「每日 ±18% 二分」近似，對抄底側偏寬鬆（混入動能雜訊）。本腳本改用 **swing-only 標註**
（鏡像加密版 relative_low_backtest 方法），讓抄底/逃頂結論更可信：

  swing low  = price[t] 為 ±W 日 centered 窗的最低（真實波段低點）
  swing high = price[t] 為 ±W 日 centered 窗的最高（真實波段高點）

  抄底：在 swing low 中，正樣本=其後60日 ≥+18%（真底）、負樣本=未達（假底/價值陷阱）
       → 測各維能否「在波段低點分辨真底 vs 假底」
  逃頂：在 swing high 中，正樣本=其後60日 ≤-18%（真頂）、負樣本=未達
       → 測各維能否「在波段高點分辨真頂 vs 假頂」

這比 S2 嚴格（樣本只在波段轉折點），AUC 更貼近實戰「在轉折點判斷」的判別力。
out-of-sample test split（≥2024）。複用 tw_dim_backtest.auc（單一來源，不重造）。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_dim_backtest import auc, per_stock_pctile   # noqa: E402

_PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_calib_panel.parquet")
_SPLIT = "2024-01-01"
_REV = 0.18
_W = 10   # swing 窗：±10 交易日（約 2 週）


def main():
    if not os.path.exists(_PANEL):
        print(f"[S2b] 找不到 panel，先跑 tw_calib_extract.py"); sys.exit(1)
    df = pd.read_parquet(_PANEL)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values(["Stock_ID", "Date"])

    # swing 偵測（centered rolling min/max；快，無 apply）
    g = df.groupby("Stock_ID", sort=False)["price"]
    win = 2 * _W + 1
    roll_min = g.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).min())
    roll_max = g.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).max())
    df["is_swing_low"] = df["price"] <= roll_min
    df["is_swing_high"] = df["price"] >= roll_max

    # PE/PB 個股分位（與 S2 同，供對照；expanding 較慢）
    print("[S2b] 計算 PE/PB 個股 expanding 分位…")
    df["PE_pctile"] = per_stock_pctile(df, "PE")
    df["PB_pctile"] = per_stock_pctile(df, "PB")

    test = df[df["Date"] >= _SPLIT]
    lows = test[test["is_swing_low"] & test["fwd_ret"].notna()].copy()
    highs = test[test["is_swing_high"] & test["fwd_ret"].notna()].copy()
    lows["real"] = lows["fwd_ret"] >= _REV       # 真底
    highs["real"] = highs["fwd_ret"] <= -_REV    # 真頂
    print(f"[S2b] test swing low {len(lows):,}（真底 {int(lows['real'].sum()):,}）｜"
          f"swing high {len(highs):,}（真頂 {int(highs['real'].sum()):,}）\n")

    # (名稱, 欄位, 抄底高分=正?, 逃頂高分=正?)
    dims = [
        ("估值 PE(絕對)",   "PE",          False, True),
        ("估值 PB(絕對)",   "PB",          False, True),
        ("估值 PE(分位)",   "PE_pctile",   False, True),
        ("估值 PB(分位)",   "PB_pctile",   False, True),
        ("槓桿 融資變化%",  "fin_chg_pct",  False, True),
        ("法人 買賣超/量",  "inst_ratio",   True,  False),
        ("大戶 major_pct",  "major_pct",    True,  False),
        ("散戶 retail_pct", "retail_pct",   False, True),
    ]

    def report(title, frame, low_side):
        print(f"══ {title}（swing-only, out-of-sample）══")
        print(f"  {'維度':<16}{'AUC':>7}{'真':>8}{'假':>8}  判讀")
        for name, col, low_dir, high_dir in dims:
            hmp = low_dir if low_side else high_dir
            a, n1, n0 = auc(frame[col], frame["real"], hmp)
            if a is None:
                print(f"  {name:<16}{'—':>7}{n1:>8}{n0:>8}  樣本不足"); continue
            v = "🟢 有訊號" if a >= 0.55 else ("🟡 弱" if a >= 0.52 else "⚪ 近雜訊")
            print(f"  {name:<16}{a:>7.3f}{n1:>8,}{n0:>8,}  {v}")
        print()

    report("抄底：swing low 中分辨真底 vs 假底", lows, True)
    report("逃頂：swing high 中分辨真頂 vs 假頂", highs, False)
    print("註：樣本限波段轉折點（比 S2 嚴）。AUC>0.55 才視為該維在轉折點有實戰判別力。"
          "TDCC 仍 2023-09 起、樣本最薄。")


if __name__ == "__main__":
    main()
