"""
scripts/tw_calib_extract.py  ·  台股維度校準 — S1 資料抽取層（離線、唯讀 climber DB）

讀 tw_stock_climber 的 SQLite（唯讀），建 panel（date×stock）供 S2 判別力分析：
  - forward 60d 報酬（Adj_Close 優先、缺則 Close）
  - PE / PB（估值，當日）
  - fin_chg_pct（融資餘額日變化% — 槓桿）
  - inst_ratio（三大法人買賣超 / 近20日均量 % — 法人）
  - tdcc major_pct / retail_pct（集保大戶/散戶，merge_asof 取 ≤ 當日最近週五；僅2023-09起，樣本薄）

輸出快取 parquet（regenerable，勿 commit）。**只讀 climber DB，不寫**。
與 watcher live 的「自包含、不 import climber」原則不衝突——此為一次性離線校準。

用法：python scripts/tw_calib_extract.py [--out <path>] [--min-date 2016-01-01]
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
_DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tw_calib_panel.parquet")
_FWD_DAYS = 60


def load_panel(db_path: str, min_date: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dq = pd.read_sql_query(
            "SELECT Date, Stock_ID, Close, Adj_Close, Volume, PE, PB, "
            "Total_Inst_BuySell, Margin_Balance FROM daily_quotes "
            "WHERE Date >= ? ORDER BY Stock_ID, Date", con, params=[min_date])
        tdcc = pd.read_sql_query(
            "SELECT Date AS tdcc_date, Stock_ID, major_pct, retail_pct "
            "FROM tdcc_holding WHERE major_pct > 0 ORDER BY Stock_ID, Date", con)
    finally:
        con.close()
    return dq, tdcc


def build_features(dq: pd.DataFrame, tdcc: pd.DataFrame) -> pd.DataFrame:
    dq["Date"] = pd.to_datetime(dq["Date"])
    dq["price"] = dq["Adj_Close"].fillna(dq["Close"])
    g = dq.groupby("Stock_ID", sort=False)
    # forward 60d 報酬
    dq["fwd_ret"] = g["price"].shift(-_FWD_DAYS) / dq["price"] - 1
    # 融資日變化%（fill_method=None：資料缺口保持 NaN，不 ffill 造假 0% 變化）
    dq["fin_chg_pct"] = g["Margin_Balance"].pct_change(fill_method=None) * 100
    # 法人 / 近20日均量
    dq["avg_vol_20"] = g["Volume"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    dq["inst_ratio"] = dq["Total_Inst_BuySell"] / dq["avg_vol_20"].where(dq["avg_vol_20"] > 0) * 100

    # TDCC merge_asof：每 (stock,date) 取 ≤ 當日最近週五的集保
    tdcc = tdcc.copy()
    tdcc["tdcc_date"] = pd.to_datetime(tdcc["tdcc_date"], format="%Y%m%d")
    tdcc = tdcc.sort_values(["Stock_ID", "tdcc_date"])
    dq = dq.sort_values(["Stock_ID", "Date"])
    merged = []
    tdcc_g = dict(tuple(tdcc.groupby("Stock_ID", sort=False)))
    for sid, sub in dq.groupby("Stock_ID", sort=False):
        t = tdcc_g.get(sid)
        if t is None or t.empty:
            sub = sub.assign(major_pct=float("nan"), retail_pct=float("nan"))
        else:
            sub = pd.merge_asof(sub, t[["tdcc_date", "major_pct", "retail_pct"]],
                                left_on="Date", right_on="tdcc_date", direction="backward")
        merged.append(sub)
    out = pd.concat(merged, ignore_index=True)
    return out[["Date", "Stock_ID", "price", "fwd_ret", "PE", "PB",
                "fin_chg_pct", "inst_ratio", "major_pct", "retail_pct"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--min-date", default="2016-01-01")
    ap.add_argument("--db", default=_CLIMBER_DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[S1] 找不到 climber DB：{args.db}"); sys.exit(1)
    print(f"[S1] 讀 {args.db}（min_date={args.min_date}）…")
    dq, tdcc = load_panel(args.db, args.min_date)
    print(f"[S1] daily_quotes {len(dq):,} 列、{dq['Stock_ID'].nunique()} 檔；tdcc {len(tdcc):,} 列")
    panel = build_features(dq, tdcc)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    try:
        panel.to_parquet(args.out, index=False)
    except Exception as e:  # pyarrow 缺 → 退 pickle
        print(f"[S1] parquet 失敗（{e}）→ 改存 pickle")
        args.out = args.out.replace(".parquet", ".pkl")
        panel.to_pickle(args.out)
    # 摘要：各特徵非空率（評估校準可用樣本）
    n = len(panel)
    print(f"[S1] panel {n:,} 列 → {args.out}")
    for c in ("fwd_ret", "PE", "PB", "fin_chg_pct", "inst_ratio", "major_pct"):
        nn = panel[c].notna().sum()
        print(f"     {c:12} 非空 {nn:>9,} ({nn/n*100:4.1f}%)")


if __name__ == "__main__":
    main()
