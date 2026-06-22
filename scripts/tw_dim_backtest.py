"""
scripts/tw_dim_backtest.py  ·  台股維度校準 — S2 判別力分析（閘門）

讀 S1 panel，量化每個台股維度對「其後 60 日報酬」的判別力（AUC），決定 S3 改不改閾值。

正負樣本（簡化版，非 swing-only）：
  抄底視角：正樣本 = fwd_ret ≥ +18%（其後大漲＝好進場點）；測各維能否分辨。
  逃頂視角：正樣本 = fwd_ret ≤ -18%（其後大跌）；測各維能否分辨。
  ⚠️ 這是「每日報酬二分」近似，非加密版的 swing 高/低點標註——會把上升趨勢中的每天都算正樣本，
     較寬鬆。作為**第一輪判別力篩**夠用（看哪些維有號、哪些是雜訊）；S3 僅在訊號明確時才動。

AUC（Mann-Whitney U / n1n2）：0.5=無判別力；>0.55 偏有訊號；維度方向已對齊（高分→該情境）。
時序 train/test split：以 2024-01-01 切，報 **test（out-of-sample）AUC** 為主，防過擬合。
PE/PB 另測「個股自身分位」(per-stock expanding rank) vs 絕對值，比較 AUC（決策2 由資料決定）。
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

_PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_calib_panel.parquet")
_SPLIT = "2024-01-01"
_REV = 0.18   # 反向 18%


def auc(values: pd.Series, target: pd.Series, higher_means_positive: bool) -> tuple:
    """維度值對二元 target 的 AUC。higher_means_positive=該維高分是否對應正樣本。"""
    m = values.notna() & target.notna()
    v, y = values[m], target[m].astype(bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 < 30 or n0 < 30:
        return None, n1, n0
    pos, neg = v[y], v[~y]
    # U：pos 值傾向大於 neg 的程度；方向不對則 1-AUC
    U = mannwhitneyu(pos, neg, alternative="two-sided").statistic
    a = U / (n1 * n0)
    if not higher_means_positive:
        a = 1 - a
    return a, n1, n0


def per_stock_pctile(df: pd.DataFrame, col: str) -> pd.Series:
    """個股自身 expanding 分位（只用當下及之前資料，無 look-ahead）。"""
    return df.groupby("Stock_ID", sort=False)[col].transform(
        lambda s: s.expanding(min_periods=60).apply(lambda w: (w.iloc[-1] >= w).mean(), raw=False))


def main():
    if not os.path.exists(_PANEL):
        print(f"[S2] 找不到 panel，先跑 tw_calib_extract.py：{_PANEL}"); sys.exit(1)
    df = pd.read_parquet(_PANEL)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values(["Stock_ID", "Date"])

    # 二元 target
    df["t_low"] = df["fwd_ret"] >= _REV       # 抄底正樣本（其後大漲）
    df["t_high"] = df["fwd_ret"] <= -_REV     # 逃頂正樣本（其後大跌）

    # PE/PB 個股分位（決策2 比較用；expanding 較慢，僅對非空列算）
    print("[S2] 計算 PE/PB 個股 expanding 分位…")
    df["PE_pctile"] = per_stock_pctile(df, "PE")
    df["PB_pctile"] = per_stock_pctile(df, "PB")

    test = df[df["Date"] >= _SPLIT]
    train = df[df["Date"] < _SPLIT]
    print(f"[S2] train {len(train):,} 列（<{_SPLIT}）｜test {len(test):,} 列（≥{_SPLIT}）\n")

    # (維度, 欄位, 抄底高分=便宜?方向, 逃頂高分=貴?方向)
    #   higher_means_positive：抄底時「該維高分對應其後大漲」、逃頂時「對應其後大跌」
    #   PE/PB 低=便宜→抄底（故 higher_means_positive_low=False，即低值→正）；高=貴→逃頂（True）
    dims = [
        ("估值 PE(絕對)",   "PE",         False, True),
        ("估值 PB(絕對)",   "PB",         False, True),
        ("估值 PE(分位)",   "PE_pctile",  False, True),
        ("估值 PB(分位)",   "PB_pctile",  False, True),
        ("槓桿 融資變化%",  "fin_chg_pct", False, True),   # 融資暴減→抄底；暴增→逃頂
        ("法人 買賣超/量",  "inst_ratio",  True,  False),  # 買超(+)→抄底；賣超(-)→逃頂
        ("大戶 major_pct",  "major_pct",   True,  False),  # 大戶高→吸籌(抄底)；散戶高→逃頂(另測)
        ("散戶 retail_pct", "retail_pct",  False, True),
    ]

    def report(title, target_col, dir_idx):
        print(f"══ {title}（out-of-sample test AUC）══")
        print(f"  {'維度':<16}{'AUC':>7}{'正樣本':>9}{'負樣本':>9}  判讀")
        for name, col, low_dir, high_dir in dims:
            hmp = low_dir if dir_idx == 2 else high_dir
            a, n1, n0 = auc(test[col], test[target_col], hmp)
            if a is None:
                print(f"  {name:<16}{'—':>7}{n1:>9}{n0:>9}  樣本不足"); continue
            verdict = "🟢 有訊號" if a >= 0.55 else ("🟡 弱" if a >= 0.52 else "⚪ 近雜訊")
            print(f"  {name:<16}{a:>7.3f}{n1:>9,}{n0:>9,}  {verdict}")
        print()

    report("抄底：其後60日 ≥+18%", "t_low", 2)
    report("逃頂：其後60日 ≤-18%", "t_high", 3)

    # TDCC 樣本警示
    tdcc_n = test["major_pct"].notna().sum()
    print(f"⚠ TDCC（major_pct）test 非空僅 {tdcc_n:,} 列、2023-09 起——樣本薄，AUC 參考性低於其他維。")
    print("註：正樣本為「每日報酬二分」近似（非 swing-only），偏寬鬆；AUC 看相對排序與是否>0.55，"
          "別當絕對命中率。S3 僅對明確>0.55 的維調整。")


if __name__ == "__main__":
    main()
