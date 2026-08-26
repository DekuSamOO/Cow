"""
tests/cycle_dim_integration.py
路線 B 續：把週期錨整合成實際維度，台股設計 → **美股單次驗收**（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/cycle_dim_integration.py            # 台股設計
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/cycle_dim_integration.py --verify   # 美股單次驗收

為什麼用「台股設計、美股驗收」而不是時間切分：
  加密逃頂的 cycle 維假說在 BTC 上的 holdout 已用掉（`tests/top_cycle_dim_calib.py`）。
  但**台股與美股的逃頂側從未加過任何 cycle 維**，兩者是互相獨立的市場、獨立的資料源，
  用其中一個設計、另一個驗收，比在同一條時間序列上再切一次乾淨。

`tests/cycle_dim_universal.py` 已證實跨市場一致性：
  價/200週均  台股 -0.338(12/13)｜美股 -0.465(7/8)｜BTC -0.422
  距 ATH      台股 -0.105(11/14)｜美股 -0.159(8/8)｜BTC -0.223
  價/2年均    台股 -0.157(7/13)｜美股 -0.222(8/8)｜BTC **+0.138** → 跨市場不一致，不採用

設計參數（在台股上訂，驗收時不得更動）：
  CYCLE 子項  價/200週均 20 + 距ATH 8 = 28（比例依 |r|，200週約為距ATH 的 2.5 倍）
  既有維度等比例縮到 72，總分刻度維持 100

驗收判準（**寫死在本檔，跑 --verify 前不得調整**）：
  C1 美股逃頂總分 AUC（波動標準化事件標記）由現行提升，且提升 >= +0.03
  C2 美股逃頂在至少一個門檻的 lift >= 1.5x（現行八檔跨標的 AUC 中位 0.500＝零訊號）
  C3 加了 cycle 維後不得讓「體制 r」變差（跨標的中位仍為負）
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from core.pit_ladder import percentile_score
from tests.radar_eval_standard import (realized_sigma_h, swing_events, auc,
                                       event_window_mask, threshold_metrics, base_rate, LEAD)

SUB = {"r_sma1400": 20, "dist_ath": 8}      # 合計 28
CYCLE_MAX = sum(SUB.values())
KEEP_SCALE = (100 - CYCLE_MAX) / 100.0      # 既有維度等比例縮到 72
MIN_OBS = 400
FWD_TD = 120

ACCEPT = """
C1 美股逃頂總分 AUC 由現行提升，且提升 >= +0.03
C2 美股逃頂在至少一個門檻的 lift >= 1.5x
C3 體制 r 跨標的中位仍為負（不得因加維而變差方向）
三條全過才算「這個維度可以落地到生產」；任一條沒過即記錄否決。
"""


def anchors(close: pd.Series) -> pd.DataFrame:
    d = pd.DataFrame(index=close.index)
    d["dist_ath"] = close / close.cummax() - 1
    d["r_sma1400"] = close / close.rolling(1400).mean()
    return d


def cycle_series(close: pd.Series) -> pd.Series:
    a = anchors(close)
    tot = pd.Series(0.0, index=close.index)
    for k, w in SUB.items():
        p = a[k].expanding(MIN_OBS).rank(pct=True) * 100
        tot += p.map(lambda x: percentile_score(None if pd.isna(x) else x, w, True))
    return tot


def fwd(c: np.ndarray, h: int) -> np.ndarray:
    n = len(c); o = np.full(n, np.nan)
    for i in range(n - h):
        o[i] = c[i + h] / c[i] - 1
    return o


def evaluate(sid, close: pd.Series, old_top: pd.Series):
    """回傳 (舊 AUC, 新 AUC, 舊體制r, 新體制r, 事件, 新分數, 舊分數)。"""
    idx = old_top.index
    close = close.reindex(idx)
    cyc = cycle_series(close).reindex(idx).fillna(0)
    new = (old_top * KEEP_SCALE + cyc).round().clip(0, 100)
    sig = realized_sigma_h(close)
    ev = swing_events(close.values, True, sigma_h=sig.values)
    n = len(idx)
    if len(ev) < 4:
        return None
    win = event_window_mask(ev, n, True)
    far = np.array([min((abs(i - e) for e in ev), default=999) > LEAD for i in range(n)])
    f = fwd(close.values, FWD_TD)
    a_old = auc(old_top.values[win], old_top.values[far])
    a_new = auc(new.values[win], new.values[far])
    ok = np.isfinite(f)
    r_old = spearmanr(old_top.values[ok], f[ok])[0]
    r_new = spearmanr(new.values[ok], f[ok])[0]
    return a_old, a_new, r_old, r_new, ev, new, old_top


def run_market(label, data, thresholds):
    """data: {sid: (close, old_top)}"""
    print()
    print("### %s" % label)
    print("  %-8s %-8s %-11s %-11s %-11s %-11s %s"
          % ("標的", "事件數", "舊 AUC", "新 AUC", "ΔAUC", "舊體制r", "新體制r"))
    A_old, A_new, R_old, R_new = [], [], [], []
    cat_s_new, cat_s_old, cat_ev, off = [], [], [], 0
    for sid, (close, old_top) in data.items():
        res = evaluate(sid, close, old_top)
        if res is None:
            print("  %-8s 事件<4，跳過" % sid); continue
        a_o, a_n, r_o, r_n, ev, new, old = res
        A_old.append(a_o); A_new.append(a_n); R_old.append(r_o); R_new.append(r_n)
        cat_s_new.append(new.values); cat_s_old.append(old.values)
        cat_ev += [e + off for e in ev]; off += len(new)
        print("  %-8s %-8d %-11.3f %-11.3f %-11s %-11.3f %.3f"
              % (sid, len(ev), a_o, a_n, "%+.3f" % (a_n - a_o), r_o, r_n))
    if not A_old:
        return None
    print("  %-8s %-8s %-11.3f %-11.3f %-11s %-11.3f %.3f"
          % ("中位", "", np.median(A_old), np.median(A_new),
             "%+.3f" % (np.median(A_new) - np.median(A_old)),
             np.median(R_old), np.median(R_new)))
    s_new = np.concatenate(cat_s_new); s_old = np.concatenate(cat_s_old)
    print()
    print("  合併門檻表（隨機基準 %.0f%%）" % (base_rate(cat_ev, len(s_new), True) * 100))
    print("  %-8s %-9s %-9s %-9s %-9s %s"
          % ("門檻", "舊觸發", "舊 lift", "新觸發", "新 lift", "新 recall"))
    best_new_lift = 0.0
    for (t, nf_o, p_o, l_o, r_o_, _, _, _), (t2, nf_n, p_n, l_n, r_n_, _, c_n, ne) in zip(
            threshold_metrics(s_old, cat_ev, True, thresholds),
            threshold_metrics(s_new, cat_ev, True, thresholds)):
        if np.isfinite(l_n):
            best_new_lift = max(best_new_lift, l_n)
        print("  %-8d %-9d %-9s %-9d %-9s %s"
              % (t, nf_o, "—" if not np.isfinite(l_o) else "%.2fx" % l_o,
                 nf_n, "—" if not np.isfinite(l_n) else "%.2fx" % l_n,
                 "—" if not np.isfinite(r_n_) else "%.0f%% (%d/%d)" % (r_n_ * 100, c_n, ne)))
    return {"auc_old": float(np.median(A_old)), "auc_new": float(np.median(A_new)),
            "r_old": float(np.median(R_old)), "r_new": float(np.median(R_new)),
            "best_lift": best_new_lift}


def tw_data():
    from tests.radar_bench_tw import TW_DB, DEFAULT_STOCKS, load_stock, replay
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)
    out = {}
    for sid in DEFAULT_STOCKS:
        loaded = load_stock(con, sid)
        if loaded is None:
            continue
        df, fin_chg, inst, pe, pb, tdcc = loaded
        sc = replay(df, fin_chg, inst, pe, pb, tdcc)
        out[sid] = (df["close"].reindex(sc.index), sc["top"].astype(float))
        print("    %s ok" % sid)
    con.close()
    return out


def us_data():
    from tests.radar_decision_bench import load_us_prices, replay_us
    out = {}
    for t, df in load_us_prices(["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN",
                                 "GOOGL", "META"]).items():
        sc = replay_us(df)
        out[t] = (df["close"].reindex(sc.index), sc["top"].astype(float))
        print("    %s ok" % t)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="跑美股單次驗收")
    args = ap.parse_args()
    print("=" * 96)
    print("路線 B 整合：cycle 維 = 價/200週均 %d + 距ATH %d（共 %d），既有維度縮到 %d"
          % (SUB["r_sma1400"], SUB["dist_ath"], CYCLE_MAX, 100 - CYCLE_MAX))
    print("=" * 96)
    if not args.verify:
        run_market("台股（設計集）", tw_data(), [15, 30, 45, 55, 65, 75])
        print()
        print("### 美股驗收判準（**已定死**）")
        print(ACCEPT)
        print("請以 --verify 跑單次驗收。")
        return
    res = run_market("美股（驗收集，從未用於設計）", us_data(), [15, 30, 45, 55, 65, 75])
    if not res:
        return
    print()
    print("### 判準檢核")
    print(ACCEPT)
    c1 = (res["auc_new"] - res["auc_old"]) >= 0.03
    c2 = res["best_lift"] >= 1.5
    c3 = res["r_new"] < 0
    print("  C1 AUC %.3f → %.3f（Δ%+.3f，需 >= +0.03）→ %s"
          % (res["auc_old"], res["auc_new"], res["auc_new"] - res["auc_old"],
             "✅通過" if c1 else "❌未通過"))
    print("  C2 新版最佳 lift = %.2fx（需 >= 1.50x）→ %s"
          % (res["best_lift"], "✅通過" if c2 else "❌未通過"))
    print("  C3 體制 r 中位 %.3f → %.3f（需仍為負）→ %s"
          % (res["r_old"], res["r_new"], "✅通過" if c3 else "❌未通過"))
    print()
    print("  最終判定：%s" % ("✅ 三條全過 → 這個維度可以落地"
                             if (c1 and c2 and c3) else "❌ 未全過 → 否決"))


if __name__ == "__main__":
    main()
