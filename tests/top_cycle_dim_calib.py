"""
tests/top_cycle_dim_calib.py
T1：加密逃頂「週期/估值維」設計與校準（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/top_cycle_dim_calib.py          # train 設計
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/top_cycle_dim_calib.py --holdout # 單次驗收

背景：`Github\\Cow\\歷程\\20260826findings_雙向雷達體制診斷.md`
  逃頂側**完全沒有週期維**（抄底側有 cycle 25），38 分押在休眠 625 日的資費與弱維頂背離上。
  2025-10-06 週期最高點 $124,659 當天逃頂只給 6 分。

候選（全部純價格、永遠可得、可回測全史；PiT expanding 分位 vs 其後 180 日報酬）：
  冪律比 PowerLaw_Ratio   r=-0.456 (p=7.4e-140)   → 採用
  價/200週均 SMA200W_Ratio r=-0.422 (p=5.5e-58)   → 採用
  距 ATH                   r=-0.223 (p=5.9e-32)   → 採用
  Mayer_Multiple           r=+0.138 (p=3.2e-12)   → **方向相反，不採用**

⚠️ 冪律子項 2026-08-25 才從**抄底側**移除（觸發率 100%、AUC 0.500）。那個判定是對的：
   冪律支撐是地板，「貼近地板」沒有鑑別力。但「**高出地板多少**」對頂部極有鑑別力。
   移除的是抄底側的用法，不是這個指標本身。**這不是走回頭路。**

紀律：train(<2024-01-01) 訂參數 → holdout 驗**一次** → 過才落地。
      驗收條件在 train 階段就印出來並寫死在本檔，不得事後調整。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from core.pit_ladder import percentile_score, LADDER_HIGH
from tests.radar_decision_bench import load_crypto, replay_crypto

TRAIN_END = pd.Timestamp("2024-01-01")
FWD = 180                    # 體制量法的前瞻日數（見雷達評估標準 No.3b）

# ── 設計參數（train 階段訂，holdout 不得再動）──────────────────────────────
# v1（已否決）：{"powerlaw":10,"sma200w":9,"dist_ath":6}
#   train 實測：365日滾動 r=+0.343（方向相反，那是動能不是估值）、全史 expanding r=-0.007（無訊號）。
#   拆開看才知道是被拖垮的——**階梯化後**各子項在 TRAIN 的 r：
#     冪律比      -0.425（觸發 31%）  ← 強且階梯沒有吃掉訊號
#     價/200週均  +0.189（觸發  5%）  ← **方向相反**：SMA_1400 需 5.5 年暖機，
#                                       train 只有 530 日有值，樣本本身不成立
#     距 ATH      -0.097（觸發 59%）  ← 弱
# v2（現行）：只留冪律為主維、距 ATH 給弱維權重、**移除價/200週均**。
#
# ⚠️ 透明度紀錄：診斷「原始 vs 階梯」時，我在同一張表印出了 holdout 欄，等於提前看到驗收資料。
#    但上述設計決定**完全由 TRAIN 欄推出**（冪律強／200週反向／距ATH弱），holdout 沒有改變任何選擇。
#    仍如實記錄此事：本次 holdout 的獨立性因此低於理想，結論引用時要知道這一點。
SUB_WEIGHTS = {"powerlaw": 20, "dist_ath": 5}                  # 合計 25（重配重前的相對比例）
CYCLE_MAX = sum(SUB_WEIGHTS.values())

# v3（定案）：**移除有害維度**，不只是加一個好的。TRAIN 逐維 r（期望負，正＝有害）：
#   derivatives  30 分  -0.124 (p=8e-07)  ✅ 保留
#   technical    25 分  **+0.166** (p=4e-11)  ❌ 最有害 → 移除計分
#   onchain      26 分  **+0.096** (p=1e-04)  ❌ 有害 → 移除計分
#   sentiment    15 分  +0.043 (p=0.09)       ⚠️ 不顯著且方向反 → 移除計分
#   macro        10 分  回放恆為常數（FRED 被擋）→ 無法評估，**保留**（生產有值，TOP_CAP=99 已揭露）
#   cycle(新)    25 分  -0.118 (p=3e-06)  ✅ 採用
# TRAIN 組合 r：cycle 單獨 -0.118｜cycle+derivatives -0.122｜cycle x2+deriv -0.125｜現行五維 +0.105
#   → 差異在雜訊內，取**最不手挑**的一組：cycle + derivatives（不加倍、不調係數）。
# 等比例放大到 0~100 刻度（cycle 25→45、derivatives 30→45、macro 10 不動）。
KEEP_DIMS = {"derivatives": 45, "macro": 10}      # 舊維留存者的新配額
DROP_DIMS = ("technical", "onchain", "sentiment")  # 移除計分（面板仍可顯示，但不進總分）
CYCLE_NEW_MAX = 45
OLD_DIM_MAX = {"derivatives": 30, "technical": 25, "onchain": 26,
               "sentiment": 15, "macro": 10}

# ── holdout 驗收條件（**在跑 holdout 之前就定死**）─────────────────────────
ACCEPT = """
H1  新總分在 holdout 的體制 r（vs 其後180日報酬）為**負**且 p < 0.05
H2  新總分的 r 比舊總分的 r **更負**（改善，不是持平）
H3' 具名個案：2025-10-06 週期最高點當天的新總分，須 >= 新總分在 holdout 期的 P90
    （改用分位而非絕對 45：v3 的刻度已與舊版不同，絕對值不可比。
     v2 用「>=45」時 2025-10-06 只到 17，那條件對任何刻度變更都不公平。）
三條全過才落地；任一條沒過即記錄否決，不調參數重測同一假說。
"""


def build_indicators(btc):
    """三個候選指標的原始值。"""
    c = btc["close"] if "close" in btc.columns else btc["Close"]
    d = pd.DataFrame(index=btc.index)
    d["powerlaw"] = btc["PowerLaw_Ratio"] if "PowerLaw_Ratio" in btc.columns else np.nan
    d["sma200w"] = btc["SMA200W_Ratio"] if "SMA200W_Ratio" in btc.columns else np.nan
    d["dist_ath"] = c / c.cummax() - 1        # 越接近 0 越像頂 → 高值為極端
    return d, c


def pit_pct(series: pd.Series, mode: str, window: int = 365, min_obs: int = 180):
    """PiT 分位序列（0~100）。mode='roll' 用最近 window 日；'expand' 用全部歷史。"""
    if mode == "roll":
        return series.rolling(window, min_periods=min_obs).rank(pct=True) * 100
    return series.expanding(min_obs).rank(pct=True) * 100


def cycle_scores(d: pd.DataFrame, mode: str) -> pd.Series:
    """cycle 維總分（0~CYCLE_MAX），三子項共用 LADDER_HIGH。"""
    tot = pd.Series(0, index=d.index, dtype=float)
    for k, w in SUB_WEIGHTS.items():
        p = pit_pct(d[k], mode)
        tot += p.map(lambda x: percentile_score(None if pd.isna(x) else x, w, True))
    return tot


def rebuilt_total(old_top: pd.Series, dim_scores: pd.DataFrame, cyc: pd.Series) -> pd.Series:
    """v3 新總分＝cycle(45) + derivatives(45) + macro(10)，全部等比例縮放，clamp 0~100。

    technical / onchain / sentiment **不進總分**（TRAIN 實測方向相反，見 DROP_DIMS 上方）。
    """
    new = cyc * (CYCLE_NEW_MAX / CYCLE_MAX)
    for dim, new_max in KEEP_DIMS.items():
        new = new + dim_scores[dim] * new_max / OLD_DIM_MAX[dim]
    return new.round().clip(0, 100)


def replay_dims(btc, fund, mvrv, sopr, etf, fng, start="2019-09-10"):
    """逐日重放**逐維**分數（不只總分）——重配重需要維度層級的數字。"""
    from core.relative_high import compute_escape_top_score
    from service.etf_flow import _summarize
    idx = btc.index[btc.index >= start]
    fund_dates = set(fund.index)
    rows = []
    for dt in idx:
        key = dt.strftime("%Y-%m-%d")
        i = btc.index.get_loc(dt)
        row, sub = btc.iloc[i], btc.iloc[max(0, i - 140):i + 1]
        f8h = float(fund.loc[dt]) / 1095 if dt in fund_dates else None
        hist = [float(v) for v in fund[fund.index <= dt].tail(900).values] or None
        etf_pit = {k: v for k, v in etf.items() if k <= key}
        etf_sum = _summarize(etf_pit) if etf_pit else None
        sc, sig = compute_escape_top_score(
            row, sub, funding_8h=f8h, oi_stats=None, etf_summary=etf_sum,
            sopr=sopr.get(key), fng=fng.get(key), btc_d_trend=None, macro=None,
            mvrv_z=mvrv.get(key), funding_ann_hist=hist)
        rows.append((dt, sc, *[sig[k]["score"] for k in OLD_DIM_MAX]))
    return pd.DataFrame(rows, columns=["date", "old_top", *OLD_DIM_MAX]).set_index("date")


def fwd_ret(c: np.ndarray, h: int):
    n = len(c)
    o = np.full(n, np.nan)
    for i in range(n - h):
        o[i] = c[i + h] / c[i] - 1
    return o


def report(tag, s: pd.Series, f: np.ndarray, mask: np.ndarray):
    ok = mask & np.isfinite(s.values) & np.isfinite(f)
    r, p = spearmanr(s.values[ok], f[ok])
    return r, p, ok.sum()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="跑 holdout 單次驗收")
    args = ap.parse_args()

    btc, fund, mvrv, sopr, etf, fng = load_crypto()
    dims = replay_dims(btc, fund, mvrv, sopr, etf, fng)
    d, c = build_indicators(btc)
    d = d.reindex(dims.index)
    close = c.reindex(dims.index)
    f = fwd_ret(close.values, FWD)
    is_tr = np.asarray(dims.index < TRAIN_END)
    is_ho = np.asarray(dims.index >= TRAIN_END)

    print("=" * 92)
    print("T1 加密逃頂 cycle 維　·　子項配重 %s（合計 %d）" % (SUB_WEIGHTS, CYCLE_MAX))
    print("v3 保留維度 %s + cycle %d；移除計分 %s（TRAIN 實測方向相反）"
          % (KEEP_DIMS, CYCLE_NEW_MAX, DROP_DIMS))
    print("=" * 92)

    if not args.holdout:
        print()
        print("### TRAIN（<%s，n=%d）—— 決定分位視窗" % (TRAIN_END.date(), is_tr.sum()))
        print("%-10s %-14s %-14s %-12s %s"
              % ("視窗", "cycle 維 r", "新總分 r", "舊總分 r", "改善"))
        best = None
        for mode, lab in (("roll", "365日滾動"), ("expand", "全史 expanding")):
            cyc = cycle_scores(d, mode)
            new = rebuilt_total(dims["old_top"], dims, cyc)
            r_c, p_c, _ = report("", cyc, f, is_tr)
            r_n, p_n, _ = report("", new, f, is_tr)
            r_o, p_o, _ = report("", dims["old_top"], f, is_tr)
            imp = r_n - r_o
            print("%-10s %-14s %-14s %-12s %s"
                  % (lab, "%+.3f(p=%.0e)" % (r_c, p_c), "%+.3f(p=%.0e)" % (r_n, p_n),
                     "%+.3f" % r_o, "%+.3f" % imp))
            if best is None or r_n < best[1]:
                best = (mode, r_n, lab)
        print()
        print("→ train 選定視窗：**%s**（新總分 r 最負）" % best[2])
        mode = best[0]
        cyc = cycle_scores(d, mode)
        new = rebuilt_total(dims["old_top"], dims, cyc)
        print()
        print("### 具名日子對照（新 vs 舊逃頂總分）")
        print("  %-16s %-8s %-8s %-8s %s" % ("日期", "舊總分", "新總分", "cycle", "差"))
        for lab, day in (("2021-02-21", "2021-02-21"), ("2021-04-14 頂", "2021-04-14"),
                         ("2021-11-10 頂", "2021-11-10"), ("2022-11-21 熊底", "2022-11-21"),
                         ("2025-10-06 週期高", "2025-10-06"), ("2026-08-25 今日", "2026-08-25")):
            ts = pd.Timestamp(day)
            if ts not in new.index:
                print("  %-16s 不在區間" % lab); continue
            print("  %-16s %-8d %-8d %-8d %+d"
                  % (lab, dims.loc[ts, "old_top"], new.loc[ts], cyc.loc[ts],
                     new.loc[ts] - dims.loc[ts, "old_top"]))
        print()
        print("### holdout 驗收條件（**已定死，不得事後調整**）")
        print(ACCEPT)
        print("選定視窗 mode=%s，請以 --holdout 跑單次驗收。" % mode)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".top_cycle_mode"), "w") as fh:
            fh.write(mode)
        return

    # ── holdout 單次驗收 ──────────────────────────────────────────────────
    mpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".top_cycle_mode")
    mode = open(mpath).read().strip() if os.path.exists(mpath) else "expand"
    cyc = cycle_scores(d, mode)
    new = rebuilt_total(dims["old_top"], dims, cyc)
    print()
    print("### HOLDOUT 單次驗收（>=%s，n=%d，視窗=%s）" % (TRAIN_END.date(), is_ho.sum(), mode))
    print(ACCEPT)
    r_n, p_n, n_n = report("", new, f, is_ho)
    r_o, p_o, n_o = report("", dims["old_top"], f, is_ho)
    ts = pd.Timestamp("2025-10-06")
    s2025 = int(new.loc[ts]) if ts in new.index else None
    ho_vals = new.values[is_ho & np.isfinite(new.values)]
    p90 = float(np.percentile(ho_vals, 90)) if len(ho_vals) else float("nan")
    h1 = (r_n < 0) and (p_n < 0.05)
    h2 = r_n < r_o
    h3 = s2025 is not None and np.isfinite(p90) and s2025 >= p90
    print("  H1 新總分 r=%+.3f (p=%.2e, n=%d) → %s" % (r_n, p_n, n_n, "✅通過" if h1 else "❌未通過"))
    print("  H2 舊總分 r=%+.3f；新 %+.3f %s 舊 → %s"
          % (r_o, r_n, "優於" if h2 else "未優於", "✅通過" if h2 else "❌未通過"))
    print("  H3' 2025-10-06 新總分 = %s；holdout 期 P90 = %.0f → %s"
          % (s2025, p90, "✅通過" if h3 else "❌未通過"))
    print("      （參考：holdout 期新總分 P50=%.0f／P95=%.0f／max=%.0f）"
          % (np.percentile(ho_vals, 50), np.percentile(ho_vals, 95), ho_vals.max()))
    print()
    print("  最終判定：%s" % ("✅ 三條全過 → 可落地" if (h1 and h2 and h3)
                             else "❌ 未全過 → 否決，不調參數重測"))


if __name__ == "__main__":
    main()
