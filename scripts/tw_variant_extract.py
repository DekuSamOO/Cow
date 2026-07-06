"""
scripts/tw_variant_extract.py  ·  台股維度 v0.3 弱維強化 — 擴充 panel 抽取（離線、唯讀 climber DB）

baseline 的 tw_calib_extract.py 只抽正規化後的維度（inst_ratio/fin_chg_pct）。
v0.3 要回測弱維「變體」，需要原始欄位重算多種候選特徵。本腳本抽出擴充 panel：

  原始法人分欄    Foreign_BuySell / Trust_BuySell / Dealer_BuySell / Total_Inst_BuySell
  融資融券        Margin_Balance / Short_Balance
  量價            Open / High / Low / Close / Adj_Close / Volume
  TDCC            major_pct / mid_pct / retail_pct / major_lots / mid_lots / retail_lots

衍生特徵（變體）在 tw_variant_backtest.py 算，本腳本只負責抽原料 + forward 報酬。
與 baseline 一致：只讀 climber DB、不寫；輸出 regenerable parquet（勿 commit）。

用法：python scripts/tw_variant_extract.py [--min-date 2016-01-01]
"""
import os
import sys
import argparse
import sqlite3

import pandas as pd

# S-3（2026-07-06）：改相對路徑，假設 tw_stock_climber 與 Cow 為同層 sibling repo；
# 可用環境變數 TW_CLIMBER_DB 覆蓋（非 sibling 佈局時）。
_CLIMBER_DB = os.getenv("TW_CLIMBER_DB") or os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "tw_stock_climber", "db", "twse_official_data.db"))
_DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_variant_panel.parquet")
_FWD_DAYS = 60


def load_raw(db_path: str, min_date: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dq = pd.read_sql_query(
            "SELECT Date, Stock_ID, Open, High, Low, Close, Adj_Close, Volume, PE, PB, "
            "Foreign_BuySell, Trust_BuySell, Dealer_BuySell, Total_Inst_BuySell, "
            "Margin_Balance, Short_Balance FROM daily_quotes "
            "WHERE Date >= ? ORDER BY Stock_ID, Date", con, params=[min_date])
        tdcc = pd.read_sql_query(
            "SELECT Date AS tdcc_date, Stock_ID, major_pct, mid_pct, retail_pct, "
            "major_lots, mid_lots, retail_lots FROM tdcc_holding "
            "WHERE major_pct > 0 ORDER BY Stock_ID, Date", con)
    finally:
        con.close()
    return dq, tdcc


def build(dq: pd.DataFrame, tdcc: pd.DataFrame) -> pd.DataFrame:
    dq["Date"] = pd.to_datetime(dq["Date"])
    dq["price"] = dq["Adj_Close"].fillna(dq["Close"])
    g = dq.groupby("Stock_ID", sort=False)
    dq["fwd_ret"] = g["price"].shift(-_FWD_DAYS) / dq["price"] - 1

    # TDCC merge_asof：每 (stock,date) 取 ≤ 當日最近週五（與 baseline 同法）
    tdcc = tdcc.copy()
    tdcc["tdcc_date"] = pd.to_datetime(tdcc["tdcc_date"], format="%Y%m%d")
    tdcc = tdcc.sort_values(["Stock_ID", "tdcc_date"])
    dq = dq.sort_values(["Stock_ID", "Date"])
    tcols = ["tdcc_date", "major_pct", "mid_pct", "retail_pct",
             "major_lots", "mid_lots", "retail_lots"]
    tdcc_g = dict(tuple(tdcc.groupby("Stock_ID", sort=False)))
    merged = []
    for sid, sub in dq.groupby("Stock_ID", sort=False):
        t = tdcc_g.get(sid)
        if t is None or t.empty:
            for c in tcols[1:]:
                sub = sub.assign(**{c: float("nan")})
        else:
            sub = pd.merge_asof(sub, t[tcols], left_on="Date", right_on="tdcc_date",
                                direction="backward")
        merged.append(sub)
    out = pd.concat(merged, ignore_index=True)
    keep = ["Date", "Stock_ID", "price", "fwd_ret", "Open", "High", "Low", "Close",
            "Volume", "PE", "PB", "Foreign_BuySell", "Trust_BuySell", "Dealer_BuySell",
            "Total_Inst_BuySell", "Margin_Balance", "Short_Balance",
            "major_pct", "mid_pct", "retail_pct", "major_lots", "mid_lots", "retail_lots"]
    return out[keep]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--min-date", default="2016-01-01")
    ap.add_argument("--db", default=_CLIMBER_DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[EXT] 找不到 climber DB：{args.db}"); sys.exit(1)
    print(f"[EXT] 讀 {args.db}（min_date={args.min_date}）…")
    dq, tdcc = load_raw(args.db, args.min_date)
    print(f"[EXT] daily_quotes {len(dq):,} 列、{dq['Stock_ID'].nunique()} 檔；tdcc {len(tdcc):,} 列")
    panel = build(dq, tdcc)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    panel.to_parquet(args.out, index=False)
    n = len(panel)
    print(f"[EXT] panel {n:,} 列 → {args.out}")
    for c in ("fwd_ret", "Foreign_BuySell", "Trust_BuySell", "Short_Balance",
              "major_lots", "retail_lots"):
        nn = panel[c].notna().sum()
        print(f"     {c:18} 非空 {nn:>9,} ({nn/n*100:4.1f}%)")


if __name__ == "__main__":
    main()
