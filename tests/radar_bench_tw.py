"""
tests/radar_bench_tw.py
台股逃頂／抄底雷達端到端基線（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/radar_bench_tw.py
  --stocks 2330,2317      指定標的（預設一籃 15 檔，含 ETF/大型/中小型）
  --json out.json         寫基線供改動後對拍

**為什麼要獨立一支**：台股雷達要吃 chip bundle（PE/PB、融資、法人、TDCC），
`scripts/data/tw_calib_panel.parquet` 只有原始維度值、沒有 bundle 結構，
既有的台股回測腳本也只量**單維** AUC，沒有人量過**總分**的門檻決策品質。
本檔直接從 climber DB 逐日組回 bundle，重放正式計分函數本身。

方法一律 import `tests/radar_eval_standard`（規格正本：vault `Github\\Cow\\雷達評估標準.md`）：
事件門檻＝該標的自身波動的 k 倍，不是固定 18%。
台股雷達是 **swing/計時導向**（`tw_swing_backtest` 明寫「在波段轉折點判斷」）
→ 主指標用 precision／lift／recall；但**同時附體制量法**（分數 vs 其後報酬），
避免重蹈加密側「用錯量法把有效雷達判成無效」的錯。

資料源：`tw_stock_climber/db/twse_official_data.db`（唯讀，公司網路可跑）
  daily_quotes  OHLCV + PE/PB + Margin_Balance + Total_Inst_BuySell
  tdcc_holding  major_pct / retail_pct（**週資料**，PiT 前向填補到日）

⚠️ 已知口徑限制（誠實列出）：
  - 用未還原權值的 Close/High/Low（與 climber 及 Yahoo v8 一致）→ 除權息跳空會進技術指標
  - TDCC 自 2021-03 才有 → 之前的日子該維一律 0 分（與線上灰燈一致）
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from core.indicators import calculate_technical_indicators
from core.relative_high_tw import compute_relative_high_tw
from core.relative_low_tw import compute_relative_low_tw
from tests.radar_eval_standard import (realized_sigma_h, swing_events, base_rate, auc,
                                       print_threshold_table, event_window_mask, LEAD)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TW_DB = os.path.abspath(os.path.join(ROOT, "..", "tw_stock_climber", "db",
                                     "twse_official_data.db"))
DIV_WINDOW = 140
WARMUP = 250
TRAIN_END = "2024-01-01"
DEFAULT_STOCKS = ["0050", "2330", "2317", "2454", "2412", "1301", "2603", "2609",
                  "3008", "6782", "2891", "1101", "2002", "3034", "2408"]


def load_stock(con, sid):
    q = pd.read_sql(
        "select Date, Open, High, Low, Close, Volume, PE, PB, Margin_Balance,"
        " Total_Inst_BuySell from daily_quotes where Stock_ID=? order by Date",
        con, params=(sid,), parse_dates=["Date"])
    if q.empty or len(q) < WARMUP + 300:
        return None
    q = q.set_index("Date")
    df = pd.DataFrame({"open": q["Open"], "high": q["High"], "low": q["Low"],
                       "close": q["Close"], "volume": q["Volume"]}).dropna(subset=["close"])
    df = calculate_technical_indicators(df)
    # 融資餘額日變化率（%）——與 service.tw_chip 的 fin_chg_pct 同義
    mb = q["Margin_Balance"].reindex(df.index)
    fin_chg = (mb / mb.shift(1) - 1) * 100
    inst = q["Total_Inst_BuySell"].reindex(df.index)
    pe, pb = q["PE"].reindex(df.index), q["PB"].reindex(df.index)
    # TDCC：週資料 → PiT 前向填補（只用 <= 當日者）
    t = pd.read_sql("select Date, major_pct, retail_pct from tdcc_holding where Stock_ID=?"
                    " order by Date", con, params=(sid,))
    if t.empty:
        tdcc = pd.DataFrame(index=df.index, columns=["major_pct", "retail_pct"], dtype=float)
    else:
        t["Date"] = pd.to_datetime(t["Date"], format="%Y%m%d")
        tdcc = t.set_index("Date").reindex(df.index, method="ffill")
    return df, fin_chg, inst, pe, pb, tdcc


def replay(df, fin_chg, inst, pe, pb, tdcc):
    rows = []
    for i in range(WARMUP, len(df)):
        d = df.index[i]
        row = df.iloc[i]
        sub = df.iloc[max(0, i - DIV_WINDOW):i + 1]
        chip = {
            "valuation": ({"pe": None if pd.isna(pe.iloc[i]) else float(pe.iloc[i]),
                           "pb": None if pd.isna(pb.iloc[i]) else float(pb.iloc[i])}
                          if not (pd.isna(pe.iloc[i]) and pd.isna(pb.iloc[i])) else None),
            "margin": ({"fin_chg_pct": float(fin_chg.iloc[i])}
                       if not pd.isna(fin_chg.iloc[i]) else None),
            "institutional": ({"total_net": float(inst.iloc[i])}
                              if not pd.isna(inst.iloc[i]) else None),
            "tdcc": ({"major_pct": None if pd.isna(tdcc["major_pct"].iloc[i]) else float(tdcc["major_pct"].iloc[i]),
                      "retail_pct": None if pd.isna(tdcc["retail_pct"].iloc[i]) else float(tdcc["retail_pct"].iloc[i])}
                     if not pd.isna(tdcc["retail_pct"].iloc[i]) else None),
        }
        rows.append((d,
                     compute_relative_high_tw(row, sub, chip=chip)[0],
                     compute_relative_low_tw(row, sub, chip=chip)[0]))
    return pd.DataFrame(rows, columns=["date", "top", "low"]).set_index("date")


def fwd_ret(close: np.ndarray, h: int):
    n = len(close)
    o = np.full(n, np.nan)
    for i in range(n - h):
        o[i] = close[i + h] / close[i] - 1
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    if not os.path.exists(TW_DB):
        print("找不到 climber DB：%s" % TW_DB)
        return
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)

    print("=" * 96)
    print("台股雷達決策基線　·　事件門檻＝波動標準化（規格：Github\\Cow\\雷達評估標準.md）")
    print("=" * 96)
    out, agg = {}, {"top": [], "low": []}
    per_stock = {}
    for sid in [s.strip() for s in args.stocks.split(",") if s.strip()]:
        loaded = load_stock(con, sid)
        if loaded is None:
            print("  %s 資料不足，跳過" % sid)
            continue
        df, fin_chg, inst, pe, pb, tdcc = loaded
        sc = replay(df, fin_chg, inst, pe, pb, tdcc)
        close = df["close"].reindex(sc.index)
        sig = realized_sigma_h(close)
        n = len(sc)
        f120 = fwd_ret(close.values, 120)
        rec = {}
        for col, is_top in (("top", True), ("low", False)):
            s = sc[col].values.astype(float)
            ev = swing_events(close.values, is_top, sigma_h=sig.values)
            win = event_window_mask(ev, n, is_top)
            far = np.array([min((abs(i - e) for e in ev), default=999) > LEAD
                            for i in range(n)]) if ev else np.ones(n, bool)
            a = auc(s[win], s[far]) if ev else float("nan")
            ok = np.isfinite(s) & np.isfinite(f120)
            r, p = spearmanr(s[ok], f120[ok]) if ok.sum() > 50 else (np.nan, np.nan)
            rec[col] = {"n_days": n, "n_events": len(ev), "auc": None if not np.isfinite(a) else round(a, 3),
                        "base_rate": None if ev == [] else round(base_rate(ev, n, is_top), 4),
                        "regime_r": None if not np.isfinite(r) else round(float(r), 3),
                        "regime_p": None if not np.isfinite(p) else float(p),
                        "event_median": float(np.median(s[ev])) if ev else None,
                        "score_p95": float(np.nanpercentile(s, 95))}
            agg[col].append((sid, rec[col]))
        per_stock[sid] = (sc, close, sig)
        out[sid] = rec
        print("  %s 完成（%d 日，%s ~ %s）" % (sid, n, sc.index[0].date(), sc.index[-1].date()))

    for col, side, is_top in (("top", "逃頂", True), ("low", "抄底", False)):
        print()
        print("【台股 %s】" % side)
        print("  %-8s %-8s %-9s %-9s %-11s %-13s %s"
              % ("標的", "事件數", "AUC", "隨機基準", "事件當天中位", "體制 r", "體制 p"))
        for sid, r in agg[col]:
            print("  %-8s %-8d %-9s %-9s %-11s %-13s %s"
                  % (sid, r["n_events"], r["auc"],
                     "—" if r["base_rate"] is None else "%.0f%%" % (r["base_rate"] * 100),
                     "—" if r["event_median"] is None else "%.0f" % r["event_median"],
                     "%+.3f" % r["regime_r"] if r["regime_r"] is not None else "—",
                     "%.1e" % r["regime_p"] if r["regime_p"] is not None else "—"))
        aucs = [r["auc"] for _, r in agg[col] if r["auc"] is not None]
        rs = [r["regime_r"] for _, r in agg[col] if r["regime_r"] is not None]
        want = "<0" if is_top else ">0"
        good = sum(1 for r in rs if (r < 0 if is_top else r > 0))
        print("  → 跨標的 AUC 中位 %.3f（0.5＝無訊號）｜體制 r 中位 %+.3f（期望 %s）｜方向正確 %d/%d"
              % (float(np.median(aucs)) if aucs else float("nan"),
                 float(np.median(rs)) if rs else float("nan"), want, good, len(rs)))

    # 全標的合併的門檻決策品質（單一標的事件太少，合併才有統計力）
    print()
    print("【台股 合併全標的：門檻決策品質】")
    for col, side, is_top, ths in (("top", "逃頂", True, [15, 30, 45, 55, 65, 75]),
                                   ("low", "抄底", False, [15, 30, 45, 55, 65, 75])):
        all_s, all_ev, off = [], [], 0
        for sid, (sc, close, sig) in per_stock.items():
            s = sc[col].values.astype(float)
            ev = swing_events(close.values, is_top, sigma_h=sig.values)
            all_s.append(s)
            all_ev += [e + off for e in ev]
            off += len(s)
        if not all_s:
            continue
        s_cat = np.concatenate(all_s)
        print()
        r = print_threshold_table("%s（%d 檔合併）" % (side, len(per_stock)),
                                  s_cat, all_ev, is_top, ths)
        out["_merged_%s" % col] = r

    con.close()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=float)
        print()
        print("基線已寫入 %s" % args.json)


if __name__ == "__main__":
    main()
