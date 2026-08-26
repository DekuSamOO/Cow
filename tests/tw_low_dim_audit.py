"""
tests/tw_low_dim_audit.py
T3：台股抄底雷達逐維體檢（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/tw_low_dim_audit.py

背景：`Github\\Cow\\歷程\\20260826findings_雙向雷達體制診斷.md` No.7b
  台股抄底端到端 **AUC 中位 0.498、合併 lift 在 15/30/45/55/65/75 全部落在 0.96~1.15x**，
  且 15 分門檻觸發 19,187 個交易日（59% 的日子）＝**常亮**。
  對照台股逃頂在 65 分有 lift 1.93x —— 同一批資料、同一套框架，抄底側就是沒有訊號。

本檔把 2026-08-25 加密側用過的「觸發率／滿分率／單維 AUC」體檢搬到台股抄底四維：
  leverage 40（宣稱最強維，融資清洗）／technical 30／institution 20／valuation 10

判準（沿用 radar_subitem_audit）：
  觸發率 0%＝死項；100%＝常亮 —— 兩者都代表配分是虛的
  AUC 0.5＝無訊號；<0.5＝方向相反
事件標記用波動標準化（k_bot=1.90，見 `Github\\Cow\\雷達評估標準.md`），不是固定 18%。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.relative_low_tw import compute_relative_low_tw, WEIGHTS_LOW_TW
from tests.radar_bench_tw import (TW_DB, DEFAULT_STOCKS, WARMUP, DIV_WINDOW,
                                  load_stock, fwd_ret)
from tests.radar_eval_standard import realized_sigma_h, swing_events, auc, event_window_mask, LEAD

TRAIN_END = pd.Timestamp("2024-01-01")


def replay_dims(df, fin_chg, inst, pe, pb, tdcc):
    """逐日重放抄底**逐維**分數。"""
    rows = []
    for i in range(WARMUP, len(df)):
        row, sub = df.iloc[i], df.iloc[max(0, i - DIV_WINDOW):i + 1]
        chip = {
            "valuation": ({"pe": None if pd.isna(pe.iloc[i]) else float(pe.iloc[i]),
                           "pb": None if pd.isna(pb.iloc[i]) else float(pb.iloc[i])}
                          if not (pd.isna(pe.iloc[i]) and pd.isna(pb.iloc[i])) else None),
            "margin": ({"fin_chg_pct": float(fin_chg.iloc[i])}
                       if not pd.isna(fin_chg.iloc[i]) else None),
            "institutional": ({"total_net": float(inst.iloc[i])}
                              if not pd.isna(inst.iloc[i]) else None),
            "tdcc": None,     # 抄底四維不吃 tdcc（2026-07 已移除，AUC 0.422 方向反）
        }
        sc, sig = compute_relative_low_tw(row, sub, chip=chip)
        rows.append((df.index[i], sc, *[sig[k]["score"] for k in WEIGHTS_LOW_TW]))
    return pd.DataFrame(rows, columns=["date", "total", *WEIGHTS_LOW_TW]).set_index("date")


def main():
    if not os.path.exists(TW_DB):
        print("找不到 climber DB：%s" % TW_DB)
        return
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)
    parts, evs, closes = [], [], []
    off = 0
    for sid in DEFAULT_STOCKS:
        loaded = load_stock(con, sid)
        if loaded is None:
            continue
        df, fin_chg, inst, pe, pb, tdcc = loaded
        d = replay_dims(df, fin_chg, inst, pe, pb, tdcc)
        close = df["close"].reindex(d.index)
        sig = realized_sigma_h(close)
        ev = swing_events(close.values, False, sigma_h=sig.values)
        parts.append(d)
        evs += [e + off for e in ev]
        closes.append(close)
        off += len(d)
        print("  %s 完成（%d 日，事件 %d 次）" % (sid, len(d), len(ev)))
    con.close()
    if not parts:
        return
    D = pd.concat(parts)
    close_all = pd.concat(closes)
    n = len(D)
    win = event_window_mask(evs, n, False)
    far = np.zeros(n, bool)
    ev_arr = np.asarray(evs)
    idx = np.arange(n)
    far[:] = True
    for e in ev_arr:
        far[max(0, e - LEAD):min(n, e + 8)] = False

    print()
    print("=" * 92)
    print("台股抄底逐維體檢（%d 檔合併，n=%d 日，事件 %d 次）" % (len(parts), n, len(evs)))
    print("=" * 92)
    print("%-14s %-7s %-10s %-10s %-10s %s"
          % ("維度", "配分", "觸發率", "滿分率", "AUC", "判定"))
    for k, mx in WEIGHTS_LOW_TW.items():
        v = D[k].values.astype(float)
        trig = float((v > 0).mean())
        full = float((v >= mx).mean())
        a = auc(v[win], v[far])
        if trig < 0.02:
            verdict = "❌ 死項（幾乎不觸發）"
        elif trig > 0.90:
            verdict = "❌ 常亮（配分是虛的）"
        elif not np.isfinite(a) or a < 0.5:
            verdict = "❌ 方向相反/無訊號"
        elif a < 0.55:
            verdict = "⚠️ 弱（AUC<0.55）"
        else:
            verdict = "✅"
        print("%-14s %-7d %-10s %-10s %-10s %s"
              % (k, mx, "%.1f%%" % (trig * 100), "%.1f%%" % (full * 100),
                 "—" if not np.isfinite(a) else "%.3f" % a, verdict))
    tv = D["total"].values.astype(float)
    print("%-14s %-7d %-10s %-10s %-10s"
          % ("總分", 100, "%.1f%%" % ((tv > 0).mean() * 100),
             "%.1f%%" % ((tv >= 65).mean() * 100), "%.3f" % auc(tv[win], tv[far])))

    print()
    print("── 分數分布 ──")
    print("  總分 P50 %.0f｜P90 %.0f｜P95 %.0f｜P99 %.0f｜max %.0f"
          % tuple(np.percentile(tv, [50, 90, 95, 99, 100])))
    for k, mx in WEIGHTS_LOW_TW.items():
        v = D[k].values.astype(float)
        print("  %-12s 配分 %2d｜P50 %2.0f｜P90 %2.0f｜max %2.0f｜貢獻總分中位的 %.0f%%"
              % (k, mx, np.percentile(v, 50), np.percentile(v, 90), v.max(),
                 np.percentile(v, 50) / max(np.percentile(tv, 50), 1) * 100))


if __name__ == "__main__":
    main()
