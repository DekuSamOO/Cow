"""
scripts/tw_volwindow_calib.py  ·  量能見頂維「幾日均量」視窗校準（swing-only, OOS）

問題：現役量能維吃**單日**成交量的個股 expanding 分位（`core.relative_high_tw.vol_pctile`）。
單日量雜訊大——一根爆量就跳滿格、隔天縮回又掉光，且 live 盤中最後一根還是未結算的
部分量。本腳本用既有 `tw_variant_panel.parquet` 面板，比較「N 日均量分位」
N∈{1,3,5,10,20} 的 swing 逃頂判別力，由資料決定 N（N=1 即現役，作對照）。

方法沿用 `tw_variant_backtest.py`（刻意不改，結果才與 v0.5 拍板數字可比）——面板路徑／
split／反向門檻／swing 窗**直接 import 該檔常數**，不複製字面值，避免日後單邊改動後
兩支腳本靜默失去可比性：
  ±10 日 centered swing 窗標高/低點、其後 60 日反向 ≥18% 為真、時序 split 2024 只報 test。
  分位定義同 `tw_dim_backtest.per_stock_pctile`（個股 expanding、含當日、無 look-ahead），
  但改 pandas 原生 `expanding().rank()`（C 實作）取代原版的 `expanding.apply` Python
  callback（446 萬列 × 5 變體用 apply 跑不完），上線前先與原版逐列比對驗證一致
  （--verify-only 可單獨跑驗證）。

雙向混淆測試（CONSTITUTION 10）：同時報抄底側 AUC。若兩側都 >0.55，代表該變體抓的是
「接下來會大動」而非方向，不可當逃頂維採用（券資比/波動率分位即因此被否決）。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_dim_backtest import auc, per_stock_pctile                    # noqa: E402
from tw_variant_backtest import _PANEL, _SPLIT, _REV, _W             # noqa: E402

_MINP = 60
_WINDOWS = (1, 3, 5, 10, 20)


def expanding_pctile(s: pd.Series, min_periods: int = _MINP) -> pd.Series:
    """個股 expanding 分位：count(歷史值 ≤ 當日值)/n，含當日、無 look-ahead。
    語意等同 `tw_dim_backtest.per_stock_pctile` 的 `(w.iloc[-1] >= w).mean()`——`method="max"`
    即「≤ 當日值的筆數」、`pct=True` 除以窗內非 NaN 筆數。
    NaN 不入母體（rank 跳過），原版 apply 則讓 NaN 稀釋分母，故僅在無 NaN 欄兩者等價；
    `_verify` 比對的 `Volume` 欄無 NaN，正是等價區間。"""
    return s.expanding(min_periods=min_periods).rank(method="max", pct=True)


def _verify(df: pd.DataFrame) -> None:
    """對排序後前三檔（固定、可重現）逐列比對本檔實作 vs 原版 expanding.apply。
    比對 `Volume`（無 NaN 欄）→ 兩者應完全一致（見 `expanding_pctile` 的 NaN 說明）。"""
    ids = sorted(df["Stock_ID"].unique())[:3]
    for sid in ids:
        sub = df[df["Stock_ID"] == sid].head(600)
        ref = per_stock_pctile(sub, "Volume")
        got = expanding_pctile(sub["Volume"])
        m = ref.notna() | got.notna()
        diff = float((ref[m].fillna(-1) - got[m].fillna(-1)).abs().max())
        print(f"[VW] verify {sid}: n={int(m.sum())} 列，max|native-ref|={diff:.2e} "
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

    # 均量分位：groupby 只建一次（446 萬列 factorize 每次約 0.14s），中間的 N 日均量不落欄
    # （4 欄 float64 約 143MB，算完分位就沒人再讀）。
    g_vol = df.groupby("Stock_ID", sort=False)["Volume"]
    for n in _WINDOWS:
        print(f"[VW] 算 {n} 日均量分位…", flush=True)
        ma = (df["Volume"] if n == 1 else
              g_vol.transform(lambda s, n=n: s.rolling(n, min_periods=n).mean()))
        df[f"volpct{n}"] = ma.groupby(df["Stock_ID"], sort=False).transform(expanding_pctile)

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
    for n in _WINDOWS:
        col = f"volpct{n}"
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
