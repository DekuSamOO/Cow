"""
scripts/tw_universal_backtest.py  ·  台股「通用量價/結構」新維校準（swing-only AUC）

針對 relative_high_tw/relative_low_tw v0.3/v0.4 疊加、**從未回測**的兩個通用維度：
  vol_price（量價背離，core.relative_universal.score_volume_price_top/bottom）
  structure（結構轉折，core.relative_universal.score_structure_top/bottom）
用與 tw_swing_backtest 相同的 swing-only 方法（±W 日 centered 窗轉折點、其後 60 日 ±18% 為
真/假）算 out-of-sample（≥2024）AUC，決定這兩維要「轉正式權重 / 標弱維 / 移除」。

同時附帶：tdcc 大戶抄底維（CLAUDE.md 記 AUC 0.423 方向反）的 swing 確認——用**已快取 panel**
（tw_calib_panel.parquet，含 major_pct）算，跳過 tw_swing_backtest 慢的 PE/PB expanding 分位。

資料源：本地 climber DB（唯讀，含 OHLC）——**公司網路可跑**，不需外部網路。
用法：python scripts/tw_universal_backtest.py [--min-date 2016-01-01] [--max-stocks N]
"""
import os
import sys
import argparse

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.relative_universal import (score_volume_price_top, score_volume_price_bottom,  # noqa: E402
                                     score_structure_top, score_structure_bottom)
from tw_dim_backtest import auc   # noqa: E402  單一來源，不重造 AUC

_CLIMBER_DB = r"D:\Users\63191\Documents\GitHub\tw_stock_climber\db\twse_official_data.db"
_PANEL = os.path.join(_ROOT, "scripts", "data", "tw_calib_panel.parquet")
_SPLIT = "2024-01-01"
_REV = 0.18
_W = 10          # swing 窗：±10 交易日（與 tw_swing_backtest 一致）
_FWD = 60
_STRUCT_WIN = 140  # structure 每點只吃近 140 列（lookback120+order4+buffer），控制單次成本


# ── 量價向量化（與 core.relative_universal 門檻一致，但 batch 版）─────────────────
def _vp_scores(g: pd.core.groupby.DataFrameGroupBy, close: pd.Series, vol: pd.Series):
    """回傳 (vp_top, vp_bottom) 分數 Series（全列向量化，門檻鏡像 relative_universal）。"""
    v5 = g.transform(lambda s: s.rolling(5).mean())
    v20 = g.transform(lambda s: s.rolling(20).mean())
    vol_ratio = (v5 / v20.where(v20 > 0))
    c5 = g.transform(lambda s: s.shift(5))
    c10 = g.transform(lambda s: s.shift(10))
    ret_now = close / c5 - 1
    ret_prior = c5 / c10 - 1

    top = pd.Series(0, index=close.index, dtype=float)
    top[(vol_ratio >= 1.3) & (ret_now < ret_prior)] = 8
    top[(vol_ratio >= 1.5) & (ret_now < ret_prior) & (ret_now < 0)] = 15
    top[vol_ratio.isna() | ret_now.isna() | ret_prior.isna()] = np.nan

    bot = pd.Series(0, index=close.index, dtype=float)
    bot[(vol_ratio <= 0.75) & (ret_now >= 0)] = 8
    bot[(vol_ratio <= 0.6) & (ret_now > 0)] = 15
    bot[vol_ratio.isna() | ret_now.isna() | ret_prior.isna()] = np.nan
    return top, bot


def _struct_at_points(df: pd.DataFrame, rows: pd.DataFrame):
    """對指定 rows（swing 點）逐點算 structure 分數（只吃近 _STRUCT_WIN 列，控成本）。
    回傳 (struct_top Series, struct_bottom Series)，index 對齊 rows。"""
    # 每檔的 OHLC（檔內整數位置 g_pos 已於 main 加好），供切窗
    by_stock = {sid: sub.reset_index(drop=True) for sid, sub in
                df[["Stock_ID", "g_pos", "high", "low", "close"]].groupby("Stock_ID", sort=False)}
    st_top, st_bot = {}, {}
    for idx, r in rows.iterrows():
        sub = by_stock.get(r["Stock_ID"])
        if sub is None:
            st_top[idx] = np.nan; st_bot[idx] = np.nan; continue
        pos = int(r["g_pos"])
        win = sub.iloc[max(0, pos - _STRUCT_WIN):pos + 1][["high", "low", "close"]]
        st_top[idx] = score_structure_top(win)["score"]
        st_bot[idx] = score_structure_bottom(win)["score"]
    return pd.Series(st_top), pd.Series(st_bot)


def _report(title, frame, cols_dirs):
    print(f"══ {title} ══")
    print(f"  {'維度':<20}{'AUC':>7}{'真':>8}{'假':>8}  判讀")
    for name, col, hmp in cols_dirs:
        a, n1, n0 = auc(frame[col], frame["real"], hmp)
        if a is None:
            print(f"  {name:<20}{'—':>7}{n1:>8}{n0:>8}  樣本不足"); continue
        v = "🟢 有訊號(≥.55)" if a >= 0.55 else ("🟡 弱(≥.52)" if a >= 0.52 else
                                                ("⚪ 近雜訊" if a >= 0.48 else "🔴 方向反(<.48)"))
        print(f"  {name:<20}{a:>7.3f}{n1:>8,}{n0:>8,}  {v}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-date", default="2016-01-01")
    ap.add_argument("--db", default=_CLIMBER_DB)
    ap.add_argument("--max-stocks", type=int, default=0, help="0=全部；除錯可限檔數")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"找不到 climber DB：{args.db}"); sys.exit(1)

    import sqlite3
    print(f"[1] 讀 climber DB（min_date={args.min_date}）…")
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT Date, Stock_ID, High, Low, Close, Adj_Close, Volume FROM daily_quotes "
            "WHERE Date >= ? ORDER BY Stock_ID, Date", con, params=[args.min_date])
    finally:
        con.close()
    if args.max_stocks:
        keep = df["Stock_ID"].drop_duplicates().head(args.max_stocks)
        df = df[df["Stock_ID"].isin(keep)]
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values(["Stock_ID", "Date"]).reset_index(drop=True)
    df["g_pos"] = df.groupby("Stock_ID", sort=False).cumcount()   # 檔內整數位置（供 structure 切窗）
    # 特徵用原始 OHLC（鏡像 production：watcher df 的 close/high/low 為未還原價）
    df["close"] = df["Close"].astype(float)
    df["high"] = df["High"].astype(float)
    df["low"] = df["Low"].astype(float)
    # 標籤/報酬用還原價（除息不造成假跳空）
    df["price"] = df["Adj_Close"].fillna(df["Close"]).astype(float)
    print(f"    {len(df):,} 列、{df['Stock_ID'].nunique()} 檔")

    g_price = df.groupby("Stock_ID", sort=False)["price"]
    df["fwd_ret"] = g_price.shift(-_FWD) / df["price"] - 1

    # 量價分數（向量化）
    print("[2] 量價分數（向量化）…")
    g_close = df.groupby("Stock_ID", sort=False)["close"]
    g_vol = df.groupby("Stock_ID", sort=False)["Volume"]
    df["vp_top"], df["vp_bottom"] = _vp_scores(g_vol, df["close"], df["Volume"].astype(float))

    # swing 標註（centered，與 tw_swing_backtest 一致）
    win = 2 * _W + 1
    roll_min = g_price.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).min())
    roll_max = g_price.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).max())
    df["is_swing_low"] = df["price"] <= roll_min
    df["is_swing_high"] = df["price"] >= roll_max

    test = df[df["Date"] >= _SPLIT]
    lows = test[test["is_swing_low"] & test["fwd_ret"].notna()].copy()
    highs = test[test["is_swing_high"] & test["fwd_ret"].notna()].copy()
    print(f"    test swing low {len(lows):,} / swing high {len(highs):,}")

    # structure 只在 swing 點算
    print("[3] structure 分數（僅 swing 點，逐點）…")
    lo_top, lo_bot = _struct_at_points(df, lows)
    hi_top, hi_bot = _struct_at_points(df, highs)
    lows["struct_bottom"] = lo_bot.reindex(lows.index)
    highs["struct_top"] = hi_top.reindex(highs.index)

    lows["real"] = lows["fwd_ret"] >= _REV
    highs["real"] = highs["fwd_ret"] <= -_REV
    print(f"    真底 {int(lows['real'].sum()):,}/{len(lows):,}｜"
          f"真頂 {int(highs['real'].sum()):,}/{len(highs):,}\n")

    print("###### 通用新維 swing-only AUC（out-of-sample ≥2024）######\n")
    _report("抄底 swing low：分辨真底 vs 假底", lows, [
        ("vol_price 量價(抄底)", "vp_bottom", True),
        ("structure 結構(抄底)", "struct_bottom", True),
    ])
    _report("逃頂 swing high：分辨真頂 vs 假頂", highs, [
        ("vol_price 量價(逃頂)", "vp_top", True),
        ("structure 結構(逃頂)", "struct_top", True),
    ])

    # ── 附帶：tdcc 大戶抄底方向確認（用快取 panel，跳過慢 pctile）──
    if os.path.exists(_PANEL):
        print("###### 附帶：tdcc 大戶抄底維方向確認（快取 panel）######\n")
        p = pd.read_parquet(_PANEL)
        p["Date"] = pd.to_datetime(p["Date"])
        p = p.sort_values(["Stock_ID", "Date"])
        gp = p.groupby("Stock_ID", sort=False)["price"]
        rmin = gp.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).min())
        rmax = gp.transform(lambda s: s.rolling(win, center=True, min_periods=_W + 1).max())
        p["is_swing_low"] = p["price"] <= rmin
        p["is_swing_high"] = p["price"] >= rmax
        pt = p[p["Date"] >= _SPLIT]
        plo = pt[pt["is_swing_low"] & pt["fwd_ret"].notna()].copy()
        phi = pt[pt["is_swing_high"] & pt["fwd_ret"].notna()].copy()
        plo["real"] = plo["fwd_ret"] >= _REV
        phi["real"] = phi["fwd_ret"] <= -_REV
        _report("抄底：大戶 major_pct 高→真底?", plo, [
            ("大戶 major_pct(抄底)", "major_pct", True),
        ])
        _report("逃頂：散戶 retail_pct 高→真頂?", phi, [
            ("散戶 retail_pct(逃頂)", "retail_pct", True),
        ])
    else:
        print(f"（無快取 panel {_PANEL}，略過 tdcc 確認；可先跑 tw_calib_extract.py）")

    print("判讀：AUC≥0.55 轉正式權重（按比例重分配）；0.48–0.55 標弱維給低權；<0.48 方向反→移除或反向。")


if __name__ == "__main__":
    main()
