"""
scripts/tw_volwindow_calib.py  ·  量能見頂維「幾日均量」視窗校準（swing-only, OOS）

問題：現役量能維吃**單日**成交量的個股 expanding 分位（`core.relative_high_tw.vol_pctile`）。
單日量雜訊大——一根爆量就跳滿格、隔天縮回又掉光，且 live 盤中最後一根還是未結算的
部分量。本腳本用既有 `tw_variant_panel.parquet` 面板，比較「N 日均量分位」
N∈{1,3,5,10,20} 的 swing 逃頂判別力，由資料決定 N（N=1 即現役，作對照）。

方法沿用 `tw_variant_backtest.py`（刻意不改，結果才與 v0.5 拍板數字可比）：
  ±10 日 centered swing 窗標高/低點、其後 60 日反向 ≥18% 為真、時序 split 2024 只報 test。
  分位定義同 `tw_dim_backtest.per_stock_pctile`（個股 expanding、含當日、無 look-ahead），
  但改 bisect 增量實作（446 萬列 × 5 變體用 expanding.apply 跑不完），上線前先與原版
  逐列比對驗證一致（--verify-only 可單獨跑驗證）。

雙向混淆測試（CONSTITUTION 10）：同時報抄底側 AUC。若兩側都 >0.55，代表該變體抓的是
「接下來會大動」而非方向，不可當逃頂維採用（券資比/波動率分位即因此被否決）。
"""
import os
import sys
from bisect import insort, bisect_right

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_dim_backtest import auc, per_stock_pctile  # noqa: E402

_PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_variant_panel.parquet")
_SPLIT = "2024-01-01"
_REV = 0.18
_W = 10
_MINP = 60
_WINDOWS = (1, 3, 5, 10, 20)


def expanding_pctile_fast(s: pd.Series, min_periods: int = _MINP) -> pd.Series:
    """個股 expanding 分位的增量實作：count(歷史值 ≤ 當日值)/n，含當日、無 look-ahead。
    語意等同 `tw_dim_backtest.per_stock_pctile` 的 `(w.iloc[-1] >= w).mean()`，
    但 O(n log n) 而非 O(n²)（NaN 不入母體，原版會讓 NaN 稀釋分母，故僅在無 NaN 欄等價）。"""
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    buf: list = []
    for i, v in enumerate(vals):
        if v != v:                      # NaN
            continue
        insort(buf, v)
        if len(buf) >= min_periods:
            out[i] = bisect_right(buf, v) / len(buf)
    return pd.Series(out, index=s.index)


def _verify(df: pd.DataFrame) -> None:
    """對隨機幾檔股票逐列比對 fast vs 原版 expanding.apply（無 NaN 欄應完全一致）。"""
    ids = sorted(df["Stock_ID"].unique())[:3]
    for sid in ids:
        sub = df[df["Stock_ID"] == sid].head(600).copy()
        ref = per_stock_pctile(sub, "Volume")
        got = expanding_pctile_fast(sub["Volume"])
        m = ref.notna() | got.notna()
        diff = float((ref[m].fillna(-1) - got[m].fillna(-1)).abs().max())
        print(f"[VW] verify {sid}: n={int(m.sum())} 列，max|fast-ref|={diff:.2e} "
              f"→ {'一致' if diff < 1e-12 else '⚠ 不一致'}")


def main():
    if not os.path.exists(_PANEL):
        print(f"[VW] 找不到 {_PANEL}，先跑 tw_variant_extract.py"); sys.exit(1)
    df = pd.read_parquet(_PANEL, columns=["Date", "Stock_ID", "price", "fwd_ret", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values(["Stock_ID", "Date"]).reset_index(drop=True)
    print(f"[VW] panel {len(df):,} 列｜{df['Stock_ID'].nunique():,} 檔｜"
          f"{df['Date'].min().date()}~{df['Date'].max().date()}")

    _verify(df)
    if "--verify-only" in sys.argv:
        return

    g = df.groupby("Stock_ID", sort=False)
    cols = []
    for n in _WINDOWS:
        src = "Volume" if n == 1 else f"vol_ma{n}"
        if n > 1:
            df[src] = g["Volume"].transform(lambda s, n=n: s.rolling(n, min_periods=n).mean())
        col = f"volpct{n}"
        print(f"[VW] 算 {n} 日均量分位…", flush=True)
        df[col] = df.groupby("Stock_ID", sort=False)[src].transform(expanding_pctile_fast)
        cols.append((n, col))

    # swing 標註（與 tw_variant_backtest 同：±10 日 centered 窗）
    gp = df.groupby("Stock_ID", sort=False)["price"]
    win = 2 * _W + 1
    roll_min = gp.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).min())
    roll_max = gp.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).max())
    df["is_swing_low"] = df["price"] <= roll_min
    df["is_swing_high"] = df["price"] >= roll_max

    test = df[df["Date"] >= _SPLIT]
    lows = test[test["is_swing_low"] & test["fwd_ret"].notna()].copy()
    highs = test[test["is_swing_high"] & test["fwd_ret"].notna()].copy()
    lows["real"] = lows["fwd_ret"] >= _REV
    highs["real"] = highs["fwd_ret"] <= -_REV
    print(f"\n[VW] test swing low {len(lows):,}（真底 {int(lows['real'].sum()):,}）｜"
          f"swing high {len(highs):,}（真頂 {int(highs['real'].sum()):,}）\n")

    print("══ 量能分位視窗掃描（swing-only, out-of-sample ≥2024）══")
    print(f"  {'視窗':<12}{'逃頂AUC':>9}{'抄底AUC':>9}{'真頂':>8}{'假頂':>8}  判讀")
    for n, col in cols:
        a_hi, n1_hi, n0_hi = auc(highs[col], highs["real"], True)    # 量能高分 → 其後大跌
        a_lo, _, _ = auc(lows[col], lows["real"], True)              # 同方向套抄底側（混淆測試）
        name = "1日（現役）" if n == 1 else f"{n}日均量"
        if a_hi is None:
            print(f"  {name:<12}{'樣本不足':>9}"); continue
        flag = "✅ >0.55" if a_hi > 0.55 else "⚪ 弱"
        if a_lo is not None and a_lo > 0.55:
            flag += "  ⚠ 抄底側也>0.55（雙向＝會大動，非方向）"
        lo_txt = "—" if a_lo is None else f"{a_lo:.3f}"
        print(f"  {name:<12}{a_hi:>9.3f}{lo_txt:>9}{n1_hi:>8,}{n0_hi:>8,}  {flag}")

    print("\n  註：抄底側 AUC 為「量能分位高 → 其後大漲」的判別力（同方向套用），"
          "\n      用於雙向混淆檢查，非該變體的抄底維採用值。")


if __name__ == "__main__":
    main()
