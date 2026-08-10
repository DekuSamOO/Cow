"""
scripts/tw_position_calib.py · 台股「三軸 composite → 建議倉位」擬合起手驗證

core/action_ensemble.compute_composite_action 的建議倉位（RIDE→60-80%、ADD→70-100%…）標
「未擬合（專家設定）」。該標籤與卡關原因源自 BTC 版本（tests/position_calib.py，2026-06）：
估值分支在回放中因 OI/ETF/SOPR 缺歷史幾乎不觸發、且 BTC 趨勢淨分→後續報酬呈 U 型非單調。

但台股的逃頂/抄底分數（relative_high_tw/low_tw）不依賴 OI/ETF/SOPR，climber DB 有全歷史可測。
本腳本用**代表性樣本**（系統抽樣、非全市場）重建歷史逐日 trend_net + escape/low 分數，餵進
與 production watcher.py 完全相同的呼叫方式（compute_trend_score → compute_relative_high_tw/
low_tw → compute_composite_action，不傳 cycle_score），檢驗各行動分類（依 pos_mid 排序）的
後續報酬是否真的隨倉位單調——這是「起手驗證」，非最終定論。

方法：
  1. climber DB 系統抽樣 N 檔（依 Stock_ID 排序等距取樣，涵蓋新舊上市/各規模）。
  2. 每檔算全歷史技術指標（core.indicators.calculate_technical_indicators，backtest_mode=True
     避免週 RSI 偷看未來）+ 融資日變化%（同 tw_calib_extract 算法）。
  3. 每 --sample-every 個交易日取一次樣（預設 5≈週頻，降低相鄰日高度自相關的虛胖樣本），
     用當下之前的 trailing window 餵三個 production 函式（與 watcher.py 呼叫方式一致）。
  4. 依 pos_mid=(pos_low+pos_high)/2 排序分桶，比較 train（<split）/test（≥split）各桶
     forward 20d/60d 報酬均值是否隨 pos_mid 遞增。

用法：python scripts/tw_position_calib.py [--max-stocks 200] [--min-date 2016-01-01]
"""
import os
import sys
import argparse
import sqlite3

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.indicators import calculate_technical_indicators           # noqa: E402
from core.trend_direction import compute_trend_score                 # noqa: E402
from core.relative_high_tw import compute_relative_high_tw           # noqa: E402
from core.relative_low_tw import compute_relative_low_tw             # noqa: E402
from core.action_ensemble import compute_composite_action            # noqa: E402

# S-3（2026-07-06）：改相對路徑，假設 tw_stock_climber 與 Cow 為同層 sibling repo；
# 可用環境變數 TW_CLIMBER_DB 覆蓋（非 sibling 佈局時）。
_CLIMBER_DB = os.getenv("TW_CLIMBER_DB") or os.path.normpath(
    os.path.join(_ROOT, "..", "tw_stock_climber", "db", "twse_official_data.db"))
_SPLIT = "2024-01-01"
_WARMUP = 260     # SMA_200(200) + 緩衝，足夠 divergence lookback=120 / vol_pctile 需 60+VOL_WINDOW-1 根
_FWD = (20, 60)   # 前瞻報酬窗（交易日）


def _pick_stock_ids(con, max_stocks: int) -> list:
    """系統抽樣（依 Stock_ID 排序等距取樣），涵蓋新舊上市/各規模，非只挑大型股。"""
    all_ids = pd.read_sql_query(
        "SELECT DISTINCT Stock_ID FROM daily_quotes ORDER BY Stock_ID", con)["Stock_ID"].tolist()
    if max_stocks >= len(all_ids):
        return all_ids
    step = len(all_ids) / max_stocks
    return [all_ids[int(i * step)] for i in range(max_stocks)]


def _load_stock_df(con, stock_id: str, min_date: str) -> pd.DataFrame:
    dq = pd.read_sql_query(
        "SELECT Date, Open, High, Low, Close, Adj_Close, Volume, PE, PB, "
        "Total_Inst_BuySell, Margin_Balance FROM daily_quotes "
        "WHERE Stock_ID = ? AND Date >= ? ORDER BY Date",
        con, params=[stock_id, min_date])
    if len(dq) < _WARMUP + max(_FWD) + 10:
        return None
    dq["Date"] = pd.to_datetime(dq["Date"])
    dq = dq.set_index("Date")
    df = pd.DataFrame({
        "open": dq["Open"].astype(float), "high": dq["High"].astype(float),
        "low": dq["Low"].astype(float), "close": dq["Close"].astype(float),
        "volume": dq["Volume"].astype(float),
    }, index=dq.index)
    df = calculate_technical_indicators(df, backtest_mode=True)
    df["PE"] = dq["PE"]
    df["PB"] = dq["PB"]
    df["total_inst"] = dq["Total_Inst_BuySell"]
    df["fin_chg_pct"] = dq["Margin_Balance"].pct_change(fill_method=None) * 100
    df["price"] = dq["Adj_Close"].fillna(dq["Close"]).astype(float)
    for n in _FWD:
        df[f"fwd_{n}"] = df["price"].shift(-n) / df["price"] - 1
    return df


def _chip_at(df: pd.DataFrame, pos: int) -> dict:
    r = df.iloc[pos]
    return {
        "valuation": {"pe": r.get("PE"), "pb": r.get("PB")},
        "margin": {"fin_chg_pct": r.get("fin_chg_pct")},
        "institutional": {"total_net": r.get("total_inst")},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-stocks", type=int, default=200)
    ap.add_argument("--min-date", default="2016-01-01")
    ap.add_argument("--sample-every", type=int, default=5, help="每 N 個交易日取一次樣（降自相關）")
    ap.add_argument("--split", default=_SPLIT)
    ap.add_argument("--db", default=_CLIMBER_DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"找不到 climber DB：{args.db}"); sys.exit(1)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ids = _pick_stock_ids(con, args.max_stocks)
    print(f"[1] 系統抽樣 {len(ids)} 檔（min_date={args.min_date}）…")

    rows = []
    n_ok = 0
    for i, sid in enumerate(ids, 1):
        try:
            df = _load_stock_df(con, sid, args.min_date)
        except Exception as e:
            print(f"    [{i}/{len(ids)}] {sid} 讀取失敗：{e}"); continue
        if df is None:
            continue
        n_ok += 1
        upper = len(df) - max(_FWD) - 1
        for pos in range(_WARMUP, upper, args.sample_every):
            row = df.iloc[pos]
            if pd.isna(row.get(f"fwd_{_FWD[0]}")) or pd.isna(row.get(f"fwd_{_FWD[-1]}")):
                continue
            win = df.iloc[max(0, pos - _WARMUP):pos + 1]
            trend_net, _ = compute_trend_score(row, win)
            chip = _chip_at(df, pos)
            esc_score, _ = compute_relative_high_tw(row, win, chip=chip)
            low_score, _ = compute_relative_low_tw(row, win, chip=chip)
            act = compute_composite_action(trend_net, esc_score, low_score)
            if act is None:
                continue
            rec = {
                "stock": sid, "date": df.index[pos], "action_key": act["action_key"],
                "pos_low": act["pos_low"], "pos_high": act["pos_high"],
                "pos_mid": (act["pos_low"] + act["pos_high"]) / 2,
            }
            for n in _FWD:
                rec[f"fwd_{n}"] = row[f"fwd_{n}"]
            rows.append(rec)
        if i % 20 == 0:
            print(f"    [{i}/{len(ids)}] 已處理…（累積樣本 {len(rows):,}）")
    con.close()

    if not rows:
        print("無有效樣本，結束。"); sys.exit(1)
    res = pd.DataFrame(rows)
    print(f"\n[2] 共 {n_ok} 檔有效歷史、{len(res):,} 筆樣本"
          f"（每 {args.sample_every} 個交易日取樣一次）\n")

    def _report(frame, title):
        print(f"══ {title}（n={len(frame):,}）══")
        g = frame.groupby("action_key").agg(
            pos_mid=("pos_mid", "first"), n=("action_key", "size"),
            fwd20_mean=("fwd_20", "mean"), fwd20_med=("fwd_20", "median"),
            fwd60_mean=("fwd_60", "mean"), fwd60_med=("fwd_60", "median"),
        ).sort_values("pos_mid")
        print(f"  {'action_key':<16}{'pos_mid':>8}{'n':>8}{'fwd20均':>9}{'fwd20中':>9}"
              f"{'fwd60均':>9}{'fwd60中':>9}")
        for key, r in g.iterrows():
            print(f"  {key:<16}{r['pos_mid']:>7.0f}%{int(r['n']):>8,}"
                  f"{r['fwd20_mean']*100:>8.1f}%{r['fwd20_med']*100:>8.1f}%"
                  f"{r['fwd60_mean']*100:>8.1f}%{r['fwd60_med']*100:>8.1f}%")
        # 單調性：pos_mid 與 fwd60_mean 的 Spearman 相關（依桶，不逐筆，避免樣本數差異誤導）
        if len(g) >= 3:
            corr = g["pos_mid"].rank().corr(g["fwd60_mean"].rank(), method="pearson")
            print(f"  → 桶級 pos_mid vs fwd60_mean 等級相關：{corr:+.2f}"
                  f"（+1=完全單調遞增、0=無關、-1=完全反向）")
        print()

    train = res[res["date"] < args.split]
    test = res[res["date"] >= args.split]
    _report(train, f"IN-SAMPLE（<{args.split}）")
    _report(test, f"OUT-OF-SAMPLE（≥{args.split}）")
    print("判讀：桶級等級相關趨近 +1 → 現行倉位階梯方向站得住腳（可考慮把 TW 這邊的「未擬合」")
    print("      轉為「已用代表性樣本驗證方向」；接近 0 或負值 → 維持「未擬合」標籤誠實反映現況。")
    print("⚠️ 本腳本為代表性子集起手驗證，非全市場定論；action_key 分桶樣本數不均可能影響穩健度。")


if __name__ == "__main__":
    main()
