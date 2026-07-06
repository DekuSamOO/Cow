"""
scripts/tw_tdcc_retest.py  ·  TDCC 維度重測（大戶週變化 vs 靜態 level）

swing 回測測過的 TDCC 是「大戶持股比 level」（major_pct），兩側皆弱（AUC 0.42–0.54）。
但 tw_stock_climber 真正的籌碼訊號是「**大戶連續增加**」（週變化趨勢），不是靜態 level。
本腳本用現有 2.7 年資料測 delta/連增版的判別力，看是否該把 TDCC 子維改用 delta。

特徵（per stock，週序列）：
  major_delta   = 大戶 major_pct 週變化（diff）
  consec_up     = 大戶連續增加週數（連續 delta>0）
標註：在 swing low/high 轉折點分辨真/假底頂（同 tw_swing_backtest），out-of-sample（≥2024）。
複用 tw_dim_backtest.auc（單一來源）。
"""
import os
import sys
import sqlite3

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_dim_backtest import auc   # noqa: E402

_PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_calib_panel.parquet")
# S-3（2026-07-06）：改相對路徑，假設 tw_stock_climber 與 Cow 為同層 sibling repo；
# 可用環境變數 TW_CLIMBER_DB 覆蓋（非 sibling 佈局時）。
_CLIMBER_DB = os.getenv("TW_CLIMBER_DB") or os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "tw_stock_climber", "db", "twse_official_data.db"))
_SPLIT = "2024-01-01"
_REV = 0.18
_W = 10


def load_tdcc_weekly() -> pd.DataFrame:
    con = sqlite3.connect(f"file:{_CLIMBER_DB}?mode=ro", uri=True)
    try:
        t = pd.read_sql_query(
            "SELECT Date, Stock_ID, major_pct FROM tdcc_holding "
            "WHERE major_pct > 0 ORDER BY Stock_ID, Date", con)
    finally:
        con.close()
    t["tdcc_date"] = pd.to_datetime(t["Date"], format="%Y%m%d")
    g = t.groupby("Stock_ID", sort=False)["major_pct"]
    t["major_delta"] = g.diff()
    # 連續增加週數（delta>0 連跑）
    up = (t["major_delta"] > 0).astype(int)
    t["consec_up"] = up.groupby(t.groupby("Stock_ID", sort=False).ngroup()).apply(
        lambda s: s * (s.groupby((s == 0).cumsum()).cumcount() + 1)).reset_index(drop=True)
    return t[["Stock_ID", "tdcc_date", "major_pct", "major_delta", "consec_up"]]


def main():
    if not os.path.exists(_PANEL):
        print("先跑 tw_calib_extract.py"); sys.exit(1)
    df = pd.read_parquet(_PANEL)[["Date", "Stock_ID", "price", "fwd_ret"]]
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values(["Stock_ID", "Date"])

    tdcc = load_tdcc_weekly().sort_values(["Stock_ID", "tdcc_date"])
    # merge_asof：每日取 ≤ 當日最近一週 TDCC 的 delta/連增
    parts, tg = [], dict(tuple(tdcc.groupby("Stock_ID", sort=False)))
    for sid, sub in df.groupby("Stock_ID", sort=False):
        t = tg.get(sid)
        if t is None or t.empty:
            sub = sub.assign(major_pct=float("nan"), major_delta=float("nan"), consec_up=float("nan"))
        else:
            sub = pd.merge_asof(sub, t[["tdcc_date", "major_pct", "major_delta", "consec_up"]],
                                left_on="Date", right_on="tdcc_date", direction="backward")
        parts.append(sub)
    df = pd.concat(parts, ignore_index=True).sort_values(["Stock_ID", "Date"])

    # swing 偵測
    g = df.groupby("Stock_ID", sort=False)["price"]
    w = 2 * _W + 1
    df["is_low"] = df["price"] <= g.transform(lambda s: s.rolling(w, center=True, min_periods=_W + 1).min())
    df["is_high"] = df["price"] >= g.transform(lambda s: s.rolling(w, center=True, min_periods=_W + 1).max())

    test = df[df["Date"] >= _SPLIT]
    lows = test[test["is_low"] & test["fwd_ret"].notna()].copy()
    highs = test[test["is_high"] & test["fwd_ret"].notna()].copy()
    lows["real"] = lows["fwd_ret"] >= _REV
    highs["real"] = highs["fwd_ret"] <= -_REV
    print(f"swing low {len(lows):,}（真底 {int(lows['real'].sum()):,}）｜"
          f"swing high {len(highs):,}（真頂 {int(highs['real'].sum()):,}）\n")

    # (名稱, 欄)：三個 TDCC 特徵皆「高分=抄底正向、逃頂負向」，方向由 low_side 決定，不另存欄
    feats = [
        ("大戶 level(major_pct)", "major_pct"),
        ("大戶 週變化(delta)",    "major_delta"),
        ("大戶 連增週數",         "consec_up"),
    ]
    for title, frame, low_side in [("抄底 真底vs假底", lows, True), ("逃頂 真頂vs假頂", highs, False)]:
        print(f"══ {title}（swing-only, out-of-sample）══")
        for name, col in feats:
            a, n1, n0 = auc(frame[col], frame["real"], low_side)
            if a is None:
                print(f"  {name:<20} 樣本不足 ({n1}/{n0})"); continue
            v = "🟢 有訊號" if a >= 0.55 else ("🟡 弱" if a >= 0.52 else "⚪ 近雜訊")
            print(f"  {name:<20} AUC {a:.3f}  真{n1:,}/假{n0:,}  {v}")
        print()
    print("結論：delta/連增 若 >0.55 → 改 TDCC 子維用週變化；若仍 <0.55 → TDCC level/delta 皆弱，維持低權。")


if __name__ == "__main__":
    main()
