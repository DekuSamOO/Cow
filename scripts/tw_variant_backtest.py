"""
scripts/tw_variant_backtest.py  ·  台股維度 v0.3 弱維強化 — 變體判別力回測（swing-only, OOS）

讀 tw_variant_panel.parquet（tw_variant_extract.py 產），對每個弱維的候選變體算
out-of-sample swing AUC（與 tw_swing_backtest.py 同方法：±10 日 centered swing 窗、
反向 ≥18%、split 2024），列表 vs 現役 baseline，看哪些變體 >0.55 且勝過現役。

防過擬合紀律（沿用 20260622）：
  - swing-only 標註、時序 split（≥2024 test）
  - 複用 tw_dim_backtest.auc（Mann-Whitney U，單一來源）
  - TDCC 變體一律標〔樣本薄 2023-09起〕
  - 不 grid、不調參，純判別力排序

變體分組：P1 法人 / P2 TDCC / P3 融資逃頂 + 新候選維（量價/波動）。
broker_cost（主力成本）表為空 → 本輪 N/A，待 climber 補資料。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_dim_backtest import auc, per_stock_pctile  # noqa: E402

_PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_variant_panel.parquet")
_SPLIT = "2024-01-01"
_REV = 0.18
_W = 10
_TDCC_START = "2023-09-01"


def streak(s: pd.Series) -> pd.Series:
    """同向連續天數（買超為正、賣超為負）。單日雜訊過濾用。"""
    sign = np.sign(s.fillna(0)).astype(int)
    out = np.zeros(len(sign), dtype=float)
    run = 0
    prev = 0
    vals = sign.to_numpy()
    for i, v in enumerate(vals):
        if v == 0:
            run = 0
        elif v == prev:
            run += v
        else:
            run = v
        out[i] = run
        prev = v
    return pd.Series(out, index=s.index)


def build_variants(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Stock_ID", "Date"]).copy()
    g = df.groupby("Stock_ID", sort=False)

    # 共用：近20日均量、流通量正規化基準
    df["avg_vol_20"] = g["Volume"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    av = df["avg_vol_20"].where(df["avg_vol_20"] > 0)

    # ── P1 法人變體 ──
    df["inst_base"] = df["Total_Inst_BuySell"] / av * 100                      # 現役（對照）
    df["inst_foreign"] = df["Foreign_BuySell"] / av * 100                      # 外資分離
    df["inst_trust"] = df["Trust_BuySell"] / av * 100                          # 投信分離
    df["inst_streak"] = g["Total_Inst_BuySell"].transform(streak)             # 連續同向天數
    for n in (5, 10, 20):
        cum = g["Total_Inst_BuySell"].transform(lambda s: s.rolling(n, min_periods=max(2, n // 2)).sum())
        df[f"inst_cum{n}"] = cum / av * 100                                    # 累積N日淨額/均量
    df["inst_turnover"] = df["Total_Inst_BuySell"] / df["Volume"].where(df["Volume"] > 0) * 100  # 佔成交比

    # ── P2 TDCC 變體（樣本薄）──
    # 週變化：merge_asof 後日值僅週變，取與 ~5 交易日前差值近似「上一週 → 本週」Δ
    df["major_chg"] = g["major_pct"].transform(lambda s: s - s.shift(5))
    df["retail_chg"] = g["retail_pct"].transform(lambda s: s - s.shift(5))

    # ── P3 融資逃頂變體 ──
    df["fin_chg_pct"] = g["Margin_Balance"].transform(lambda s: s.pct_change(fill_method=None)) * 100  # 現役
    df["short_margin"] = df["Short_Balance"] / df["Margin_Balance"].where(df["Margin_Balance"] > 0)     # 券資比
    df["margin_pctile"] = per_stock_pctile(df, "Margin_Balance")               # 融資餘額個股分位

    # ── 新候選維（OHLCV 內，broker_cost 空表故略主力成本）──
    df["ret1"] = g["price"].transform(lambda s: s.pct_change(fill_method=None))
    df["volat20"] = g["ret1"].transform(lambda s: s.rolling(20, min_periods=10).std())
    df["volat_pctile"] = per_stock_pctile(df, "volat20")                       # 波動率分位
    df["vol_ratio"] = df["Volume"] / av                                        # 爆量倍數（/均量）
    df["vol_pctile"] = per_stock_pctile(df, "Volume")                          # 成交量分位
    return df


def main():
    if not os.path.exists(_PANEL):
        print(f"[VB] 找不到 {_PANEL}，先跑 tw_variant_extract.py"); sys.exit(1)
    df = pd.read_parquet(_PANEL)
    df["Date"] = pd.to_datetime(df["Date"])
    print("[VB] 建變體特徵（含 expanding 分位，稍慢）…")
    df = build_variants(df)

    g = df.groupby("Stock_ID", sort=False)["price"]
    win = 2 * _W + 1
    roll_min = g.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).min())
    roll_max = g.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).max())
    df["is_swing_low"] = df["price"] <= roll_min
    df["is_swing_high"] = df["price"] >= roll_max

    test = df[df["Date"] >= _SPLIT]
    lows = test[test["is_swing_low"] & test["fwd_ret"].notna()].copy()
    highs = test[test["is_swing_high"] & test["fwd_ret"].notna()].copy()
    lows["real"] = lows["fwd_ret"] >= _REV
    highs["real"] = highs["fwd_ret"] <= -_REV
    print(f"[VB] test swing low {len(lows):,}（真底 {int(lows['real'].sum()):,}）｜"
          f"swing high {len(highs):,}（真頂 {int(highs['real'].sum()):,}）\n")

    # (組, 名稱, 欄位, 抄底高分=正?, 逃頂高分=正?, 註)
    variants = [
        ("P1法人", "現役 買賣超/均量",   "inst_base",     True,  False, ""),
        ("P1法人", "外資 買賣超/均量",   "inst_foreign",  True,  False, ""),
        ("P1法人", "投信 買賣超/均量",   "inst_trust",    True,  False, ""),
        ("P1法人", "連續同向天數",       "inst_streak",   True,  False, ""),
        ("P1法人", "累積5日/均量",       "inst_cum5",     True,  False, ""),
        ("P1法人", "累積10日/均量",      "inst_cum10",    True,  False, ""),
        ("P1法人", "累積20日/均量",      "inst_cum20",    True,  False, ""),
        ("P1法人", "買賣超佔成交比",     "inst_turnover", True,  False, ""),
        ("P2集保", "現役 大戶比(絕對)",  "major_pct",     True,  False, "薄"),
        ("P2集保", "現役 散戶比(絕對)",  "retail_pct",    False, True,  "薄"),
        ("P2集保", "大戶比週Δ",          "major_chg",     True,  False, "薄"),
        ("P2集保", "散戶比週Δ",          "retail_chg",    False, True,  "薄"),
        ("P3融資", "現役 融資變化%",     "fin_chg_pct",   False, True,  ""),
        ("P3融資", "券資比",             "short_margin",  False, True,  "?向"),
        ("P3融資", "融資餘額分位",       "margin_pctile", False, True,  ""),
        ("新候選", "波動率分位",         "volat_pctile",  False, True,  "?向"),
        ("新候選", "爆量倍數/均量",      "vol_ratio",     False, True,  "?向"),
        ("新候選", "成交量分位",         "vol_pctile",    False, True,  "?向"),
    ]

    def report(title, frame, low_side):
        print(f"══ {title}（swing-only, out-of-sample）══")
        print(f"  {'組':<7}{'變體':<18}{'AUC':>7}{'真':>7}{'假':>7}  判讀")
        for grp, name, col, low_dir, high_dir, tag in variants:
            if col not in frame.columns:
                continue
            hmp = low_dir if low_side else high_dir
            a, n1, n0 = auc(frame[col], frame["real"], hmp)
            if a is None:
                print(f"  {grp:<7}{name:<18}{'—':>7}{n1:>7}{n0:>7}  樣本不足"); continue
            v = "🟢有訊號" if a >= 0.55 else ("🟡弱" if a >= 0.52 else "⚪雜訊")
            t = f" {tag}" if tag else ""
            print(f"  {grp:<7}{name:<18}{a:>7.3f}{n1:>7,}{n0:>7,}  {v}{t}")
        print()

    report("抄底：swing low 真底 vs 假底", lows, True)
    report("逃頂：swing high 真頂 vs 假頂", highs, False)
    print("註：?向＝方向假設未定（看 |AUC-0.5|，<0.5 代表反向才有訊號）。"
          "薄＝TDCC 2023-09起樣本薄。broker_cost 空表→主力成本維本輪 N/A。")


if __name__ == "__main__":
    main()
