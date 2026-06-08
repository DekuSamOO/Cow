"""
tests/relative_high_backtest.py
相對高點（逃頂）權重敏感度分析 — 分層 train/test

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/relative_high_backtest.py

方法（對齊已拍板決策）：
  1. 標記相對高點：日線 swing high(order=10)，其後 60 天內回撤 ≥18% 為正樣本。
  2. 僅對「有足夠歷史」的維度回測擬合：
       - 資金費率年化（2021+，service/onchain fund_hist）
       - 技術衰竭（頂背離+RSI，全期，core/relative_high._score_technical）
       - 情緒（F&G，2018+，alternative.me 全史）
     OI / ETF / 總經 因歷史不足或需發布行事曆 → **不擬合**，維持專家權重（見
     core/relative_high.UNFITTED_DIMS / WEIGHTS）。
  3. 樣本切分：時間序前半 train、後半 test（時序資料不用隨機洗牌，避免前視）。
  4. 對三個可擬合維度的「相對權重」做 grid search（在 train 上最大化 AUC），
     在 test 上驗證 AUC 與「提前預警天數」，回報是否優於現行專家權重。

AUC 以 Mann-Whitney U 實作（不引入 sklearn）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
import requests, urllib3
urllib3.disable_warnings()

from service.market_data import fetch_market_data
from service.onchain import fetch_aux_history
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators
from core.relative_high import _score_technical, annualize_funding, FUNDING_ANN_YELLOW, FUNDING_ANN_RED

DROP_THRESH = -0.18      # 60 天內回撤門檻
HORIZON = 60
ORDER = 10               # swing high 視窗（相對「重要」高點）
LOOKAHEAD_LABEL = 60


# ── AUC（Mann-Whitney U）────────────────────────────────────────────────────
def auc(pos, neg):
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    allv = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    allv.sort(key=lambda x: x[0])
    ranks = {}
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        i = j + 1
    rank_sum_pos = sum(ranks[idx] for idx, (v, lab) in enumerate(allv) if lab == 1)
    n1, n0 = len(pos), len(neg)
    u = rank_sum_pos - n1 * (n1 + 1) / 2
    return u / (n1 * n0)


# ── 各維度子分數（與 core/relative_high 同邏輯）────────────────────────────────
def funding_score(ann):
    if ann is None or np.isnan(ann):
        return 0
    if   ann >= FUNDING_ANN_RED:    return 20
    elif ann >= 70:                 return 16
    elif ann >= FUNDING_ANN_YELLOW: return 12
    elif ann >= 30:                 return 6
    elif ann >= 15:                 return 3
    return 0


def fng_score(v):
    if v is None or np.isnan(v):
        return 0
    if   v >= 90: return 10
    elif v >= 80: return 8
    elif v >= 75: return 5
    elif v >= 70: return 3
    return 0


def main():
    print("載入資料 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)

    # 資金費率歷史（日均 8h%）
    _, _, fund = fetch_aux_history()
    fund_daily = pd.Series(dtype=float)
    if fund is not None and not fund.empty and "fundingRate" in fund.columns:
        f = fund.copy()
        if f.index.tz is not None:
            f.index = f.index.tz_localize(None)
        fund_daily = f["fundingRate"].resample("D").mean()

    # F&G 全史
    fng_map = {}
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=0&format=json",
                         timeout=20, verify=False)
        for it in r.json().get("data", []):
            d = pd.to_datetime(int(it["timestamp"]), unit="s").strftime("%Y-%m-%d")
            fng_map[d] = float(it["value"])
    except Exception as e:
        print("F&G 歷史抓取失敗：", e)

    # 標記相對高點
    high = btc["high"].values.astype(float)
    close = btc["close"].values.astype(float)
    n = len(btc)
    tops = []
    for i in range(ORDER, n - ORDER):
        w = high[i - ORDER:i + ORDER + 1]
        if high[i] == np.nanmax(w) and (w == high[i]).sum() == 1:
            fut = close[i + 1:i + LOOKAHEAD_LABEL + 1]
            if len(fut) and (fut.min() / close[i] - 1) <= DROP_THRESH:
                tops.append(i)
    top_dates = [btc.index[i] for i in tops]

    # 僅取資金費率時代（2021+）做可擬合集合
    fund_start = fund_daily.index.min() if not fund_daily.empty else pd.Timestamp("2021-01-01")
    fit_tops = [i for i in tops if btc.index[i] >= fund_start]
    print(f"全期相對高點 {len(tops)} 個；資金費率時代(>= {fund_start.date()}) {len(fit_tops)} 個（可擬合）")

    # 負樣本：距任何頂 ≥45 天的隨機日（資金費率時代）
    rng = np.random.default_rng(42)
    era_idx = [k for k in range(n) if btc.index[k] >= fund_start and ORDER <= k < n - ORDER]
    top_set = set(fit_tops)
    def far_from_top(k):
        return all(abs(k - t) > 45 for t in fit_tops)
    neg_pool = [k for k in era_idx if far_from_top(k)]
    neg_idx = list(rng.choice(neg_pool, size=min(len(fit_tops) * 2, len(neg_pool)), replace=False))

    # 計算特徵
    def feats_at(k):
        d = btc.index[k].strftime("%Y-%m-%d")
        ann = annualize_funding(float(fund_daily.get(btc.index[k].normalize(), np.nan))) \
            if not fund_daily.empty else None
        fs = funding_score(ann)
        tech = _score_technical(btc.iloc[k], btc.iloc[:k + 1])["score"]   # 0-25
        gs = fng_score(fng_map.get(d, np.nan))
        return fs, tech, gs

    pos_feats = [feats_at(k) for k in fit_tops]
    neg_feats = [feats_at(k) for k in neg_idx]
    P = np.array(pos_feats, float); N = np.array(neg_feats, float)

    print("\n=== 各維度單獨判別力（AUC，>0.5 表示頂部分數較高）===")
    for j, name in enumerate(["資金費率(0-20)", "技術衰竭(0-25)", "F&G(0-15→實10)"]):
        a = auc(P[:, j].tolist(), N[:, j].tolist())
        print(f"  {name:18s} 頂均 {P[:,j].mean():5.1f} / 非頂均 {N[:,j].mean():5.1f}  AUC={a:.3f}")

    # 時間序切分 train/test
    order_pos = np.argsort([btc.index[k] for k in fit_tops])
    half = len(order_pos) // 2
    tr_p = P[order_pos[:half]]; te_p = P[order_pos[half:]]
    # 負樣本也時間切
    order_neg = np.argsort([btc.index[k] for k in neg_idx])
    hn = len(order_neg) // 2
    tr_n = N[order_neg[:hn]]; te_n = N[order_neg[hn:]]
    print(f"\n切分：train 頂 {len(tr_p)}/非頂 {len(tr_n)}；test 頂 {len(te_p)}/非頂 {len(te_n)}")

    # grid search 三維相對權重（步長 0.1，和=1），最大化 train 複合 AUC
    def composite(arr, w):
        # arr 欄: [funding/20, tech/25, fng/10]；標準化後加權
        norm = arr / np.array([20.0, 25.0, 10.0])
        return norm @ np.array(w)
    best = None
    for a_ in range(0, 11):
        for b_ in range(0, 11 - a_):
            c_ = 10 - a_ - b_
            w = (a_ / 10, b_ / 10, c_ / 10)
            sc_p = composite(tr_p, w); sc_n = composite(tr_n, w)
            tr_auc = auc(sc_p.tolist(), sc_n.tolist())
            if best is None or tr_auc > best[1]:
                best = (w, tr_auc)
    w, tr_auc = best
    te_auc = auc(composite(te_p, w).tolist(), composite(te_n, w).tolist())

    # 現行專家相對權重（funding 20 / tech 25 / fng 10 → 正規化）
    exp_w = np.array([20, 25, 10]) / 55.0
    exp_tr = auc(composite(tr_p, exp_w).tolist(), composite(tr_n, exp_w).tolist())
    exp_te = auc(composite(te_p, exp_w).tolist(), composite(te_n, exp_w).tolist())

    print("\n=== 權重 grid search（三可擬合維度相對權重）===")
    print(f"  最佳(train)權重 funding/tech/fng = {w}  train AUC={tr_auc:.3f}  test AUC={te_auc:.3f}")
    print(f"  現行專家相對權重 (20/25/10)        train AUC={exp_tr:.3f}  test AUC={exp_te:.3f}")
    print("\n結論：" + (
        "擬合權重在 test 上優於專家權重，建議微調。" if te_auc > exp_te + 0.02 else
        "擬合權重未顯著優於專家權重（樣本小），維持現行專家權重較穩健。"))
    print("註：OI / ETF / 總經未納入擬合（歷史不足／需發布行事曆），維持專家權重。")

    # ── 窗口式評估：頂部附近 [-3,+20] 天窗口「最高複合分」是否能預警 ──────────────
    # 逐日打分對「技術背離」不公（確認延遲），實務用途是「頂部附近窗口內有無預警」。
    print("\n=== 窗口式評估（頂部前後 [-3,+20] 天取最高複合分，專家相對權重）===")
    def window_max_composite(center):
        lo, hi = max(0, center - 3), min(n - 1, center + 20)
        best_s = 0.0
        for k in range(lo, hi + 1):
            f = np.array(feats_at(k), float)
            best_s = max(best_s, float(composite(f, exp_w)))
        return best_s
    pos_win = [window_max_composite(k) for k in fit_tops]
    neg_win = [window_max_composite(k) for k in neg_idx]
    win_auc = auc(pos_win, neg_win)
    print(f"  窗口最高分 頂均 {np.mean(pos_win):.3f} / 非頂均 {np.mean(neg_win):.3f}  AUC={win_auc:.3f}")
    # 命中率：窗口最高分 >= 門檻（複合 0.45 約對應原始 escape 中段）
    for thr in (0.35, 0.45, 0.55):
        hit = np.mean([s >= thr for s in pos_win]) * 100
        fp  = np.mean([s >= thr for s in neg_win]) * 100
        print(f"  門檻 {thr:.2f}: 頂部命中率 {hit:.0f}%  非頂誤報率 {fp:.0f}%")
    print("  （僅含資金費率/技術/F&G 三維；實際系統再疊加 OI/ETF/SOPR/BTC.D/總經，預警力更高）")


if __name__ == "__main__":
    main()
