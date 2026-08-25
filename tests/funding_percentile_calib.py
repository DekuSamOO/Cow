"""
tests/funding_percentile_calib.py
資金費率計分重校（2026-08-25）— 絕對年化階梯 vs PiT 滾動分位，逃頂／抄底雙側。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/funding_percentile_calib.py

資料來源＝ db/cache/ 的兩份**本機**快取（該目錄在 .gitignore 內、**未進版控**）：
  funding_rate_history.csv ← service/funding_history（缺檔時本腳本會自動連幣安重建，約 10 秒）
  BTC_HISTORY.csv          ← service/market_data.fetch_market_data（缺檔時請先跑一次 dashboard
                              或 fetch_market_data() 產生；本腳本不代抓日線）

動機：
  幣安 BTCUSDT 自 2024-12-10 之後 623 日再未越過利率基準 0.01%/8h（＝年化 10.95%，
  premiumIndex.interestRate 實查值）。全分布壓在 [-11%, +11%] 年化 →
    逃頂側 funding_threshold_calib 訂的絕對門檻（≥12/20/30/50%）命中 **0 日**；
    抄底側 ≤-20% 命中 0 日、≤-10% 僅 3 日 → 負費率子項只有 15.4% 的日子非零。
  問題：這是「指標失效該換設計」還是「市場真的沒泡沫、0 分才對」？本腳本用資料判。

方法（沿用 funding_threshold_calib 的既有方法論，僅換受測變數）：
  樣本  = 日均年化資費 + 日線；頂 = swing high(order=10) 且其後 60 日回撤 ≥18%，底反之。
  對照  = 非頂/非底（距事件 >30 日）隨機抽 3 倍，固定 seed=0。
  指標  = Mann-Whitney AUC；另跑「雙向混淆檢定」：逃頂分數若對底部也 >0.55 → 移動幅度混淆。
  切分  = train 2019-09~2023-12 選參數；holdout 2024-01~2026-08 **只驗一次**。
          （揭露：W 的四個候選值 holdout 數字是同一次跑出來的，選擇只用 train。）

拍板結論（已落地）：
  逃頂（core/relative_high）：**維持絕對階梯，否決分位法**。
      逃頂AUC 絕對 vs 分位 ── train 0.706/0.618、holdout 0.580/0.492、新環境內部 0.457/0.393。
      三處全輸 → 「連 624 日 0 分」是正確回報「無槓桿泡沫」，不是失效。只加休眠標示不動分數。
  抄底（core/relative_low）：**同樣維持絕對階梯——混合版提出後於同日撤回**。
      初版宣稱 holdout 混合 0.546 vs 絕對 0.487，獨立檢核後修掉兩個缺陷即翻盤：
        (a) 未來窗不足 60 日的樣本未剔除（holdout 16 個正樣本有 3 個只有 11~55 日窗）
        (b) 第五檔 cutoff 寫 25.0，不是稀有度預算換算值（ann<0 的 train 稀有度＝12.96%）
      歸因：(a) 單獨修 → 混合仍贏 0.610/0.499；(b) 單獨修 → 混合就輸 0.509/0.521
            → **優勢全來自那個自選參數**。兩項都修後 holdout 混合 0.486 vs 絕對 0.499，
            換 8 個對照抽樣種子 **0/8 混合勝**。
      → 依 CONSTITUTION 第 11 條不予採用；分位僅留 sub["funding_pct"] 觀測值，不計分不進 label。
  ⚠️ 重議前提：資料長大後重跑本腳本並**過 holdout**，不可只看 train（train 是選參數的那份）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.relative_low import (funding_pit_percentile, FUNDING_PCT_WINDOW,
                               FUNDING_PCT_MIN_OBS, FUNDING_PCT_LOW_CUTS)
from core.relative_high import FUNDING_ANN_BASELINE
from service.funding_history import load_funding_ann_daily

H, ORDER, MOVE = 60, 10, 0.18
TRAIN_END = "2024-01-01"
ERA_START = "2024-12-10"          # 最後一次越過基準（2024-12-09 日均）的隔日
BTC_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "db", "cache", "BTC_HISTORY.csv")


def auc(pos, neg):
    """Mann-Whitney U → AUC（與 funding_threshold_calib 同實作，含同值 midrank）。"""
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    a = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg], key=lambda x: x[0])
    ranks, i = {}, 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[j + 1][0] == a[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    rs = sum(ranks[k] for k, (v, lbl) in enumerate(a) if lbl == 1)
    return (rs - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def load():
    ann = load_funding_ann_daily(refresh=False)          # 先走純本機（可重跑、零網路）
    if ann.empty:
        print("資費快取不存在 → 連幣安重建 db/cache/funding_rate_history.csv …")
        ann = load_funding_ann_daily(refresh=True)
        if ann.empty:
            raise SystemExit("資費歷史取得失敗（網路？）→ 無法校準")
    if not os.path.exists(BTC_CSV):
        raise SystemExit(f"找不到日線快取 {BTC_CSV} → 先跑一次 "
                         "service.market_data.fetch_market_data() 產生後再校準")
    btc = pd.read_csv(BTC_CSV, index_col=0, parse_dates=True)
    idx = ann.index.intersection(btc.index)
    return ann.reindex(idx), btc.reindex(idx), idx


def rolling_pct(vals, window=FUNDING_PCT_WINDOW):
    """逐日 PiT 分位（只用當日與之前）— 直接呼叫正式計分的同一支純函數，杜絕兩套實作漂移。"""
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        p = funding_pit_percentile(vals[max(0, i - window + 1):i + 1],
                                   min_obs=FUNDING_PCT_MIN_OBS)
        if p is not None:
            out[i] = p
    return out


def swings(series, close, n, is_top, lo=0, hi=None):
    hi = n if hi is None else hi
    out = []
    for i in range(max(ORDER, lo), min(n - ORDER, hi)):
        w = series[i - ORDER:i + ORDER + 1]
        ext = (series[i] == np.nanmax(w)) if is_top else (series[i] == np.nanmin(w))
        if not ext:
            continue
        fut = close[i + 1:i + H + 1]
        # 未來窗不足 H 日就不標記：序列末端的樣本會用「不完整的視窗」判正負樣本
        # （2026-08-25 獨立檢核 🟠 No.4：holdout 16 個正樣本有 3 個只有 11~55 日窗）
        if len(fut) < H:
            continue
        mv = (fut.min() / close[i] - 1) if is_top else (fut.max() / close[i] - 1)
        if (mv <= -MOVE) if is_top else (mv >= MOVE):
            out.append(i)
    return out


def sample(high, low, close, n, lo, hi, seed=0):
    rng = np.random.default_rng(seed)
    tp = swings(high, close, n, True, lo, hi)
    bt = swings(low, close, n, False, lo, hi)
    rr = range(max(ORDER, lo), min(n - ORDER, hi))
    ntp = [k for k in rr if all(abs(k - t) > 30 for t in tp)]
    nbt = [k for k in rr if all(abs(k - t) > 30 for t in bt)]
    return (tp, list(rng.choice(ntp, size=min(len(tp) * 3, len(ntp)), replace=False)),
            bt, list(rng.choice(nbt, size=min(len(bt) * 3, len(nbt)), replace=False)))


def score_abs_low(a):
    """現行絕對階梯（core/relative_low._score_derivatives_low 的負費率側）。"""
    if a is None or np.isnan(a):
        return 0
    if a <= -20:
        return 10
    if a <= -10:
        return 8
    if a <= -5:
        return 6
    if a <= -2:
        return 3
    if a < 0:
        return 1
    return 0


def score_pct_low(p):
    """分位階梯（cutoff 由 train 稀有度預算換算，見 FUNDING_PCT_LOW_CUTS）。"""
    if p is None or np.isnan(p):
        return 0
    c10, c8, c6, c3, c1 = FUNDING_PCT_LOW_CUTS
    if p <= c10:
        return 10
    if p <= c8:
        return 8
    if p <= c6:
        return 6
    if p <= c3:
        return 3
    if p <= c1:
        return 1
    return 0


def main():
    ann, btc, idx = load()
    av = ann.values
    close, high, low = btc["close"].values, btc["high"].values, btc["low"].values
    n = len(av)
    split = int(np.searchsorted(idx.values, np.datetime64(TRAIN_END)))
    era = int(np.searchsorted(idx.values, np.datetime64(ERA_START)))
    pct = rolling_pct(av)
    print("資料 %d 日：%s ~ %s｜train 0~%d｜holdout %d~%d｜新環境自 %s"
          % (n, idx.min().date(), idx.max().date(), split - 1, split, n - 1, idx[era].date()))

    print("\n" + "=" * 78)
    print("一、新環境現況（%d 日）— 利率基準年化 %.2f%%" % (n - era, FUNDING_ANN_BASELINE))
    print("=" * 78)
    e = av[era:]
    print("  最大 %+.2f%%｜最小 %+.2f%%｜負值 %d 日 (%.1f%%)"
          % (e.max(), e.min(), (e < 0).sum(), (e < 0).mean() * 100))
    print("  逃頂絕對門檻命中： " + " ".join("≥%d%%:%d日" % (t, (e >= t).sum()) for t in (12, 20, 30, 50)))
    print("  抄底絕對門檻命中： " + " ".join("≤%d%%:%d日" % (t, (e <= t).sum()) for t in (-2, -5, -10, -20)))

    print("\n" + "=" * 78)
    print("二、逃頂側：絕對年化 vs PiT 分位（分位法是否該取代絕對階梯）")
    print("=" * 78)
    print("%-10s%-9s%-11s%-11s%s" % ("期間", "頂樣本", "絕對AUC", "分位AUC", "混淆檢定(逃頂分數對底部)"))
    for lbl, lo, hi in [("train", 0, split), ("holdout", split, n), ("新環境", era, n)]:
        tp, ntp, bt, nbt = sample(high, low, close, n, lo, hi)
        if len(tp) < 3 or len(bt) < 3:
            continue
        conf = auc([av[i] for i in bt], [av[i] for i in nbt])
        print("%-12s%-9d%-13.3f%-13.3f%.3f %s"
              % (lbl, len(tp), auc([av[i] for i in tp], [av[i] for i in ntp]),
                 auc([pct[i] for i in tp], [pct[i] for i in ntp]),
                 conf, "⚠混淆" if conf > 0.55 else "OK(方向相反)"))
    print("  → 分位法三處全輸 → 否決；逃頂維持絕對階梯（relative_high 不動分數，只加休眠標示）")

    print("\n" + "=" * 78)
    print("三、抄底側：整數分數 AUC（絕對 / 分位 / 混合取大值）")
    print("=" * 78)
    sa = np.array([score_abs_low(a) for a in av])
    sp = np.array([score_pct_low(p) for p in pct])
    sh = np.maximum(sa, sp)
    print("%-10s%-9s%-10s%-10s%-10s" % ("期間", "底樣本", "絕對", "分位", "混合"))
    for lbl, lo, hi in [("train", 0, split), ("holdout", split, n)]:
        _, _, bt, nbt = sample(high, low, close, n, lo, hi)
        print("%-12s%-9d%-12.3f%-12.3f%-12.3f"
              % (lbl, len(bt), auc([sa[i] for i in bt], [sa[i] for i in nbt]),
                 auc([sp[i] for i in bt], [sp[i] for i in nbt]),
                 auc([sh[i] for i in bt], [sh[i] for i in nbt])))
    print("\n  新環境負費率子項分數分布：")
    for name, s in [("絕對(採用)", sa), ("分位(未採用)", sp), ("混合(已撤回)", sh)]:
        sub = s[era:]
        dist = {int(k): int(v) for k, v in zip(*np.unique(sub, return_counts=True))}
        print("    %-12s非零 %5.1f%%｜平均 %.2f 分｜%s"
              % (name, np.mean(sub > 0) * 100, sub.mean(), dist))
    print("")
    print("  → **維持絕對階梯**；混合版已撤回（holdout 混合 < 絕對，8 個對照種子 0/8 勝）")
    print("     分位參數僅供未來重議：W=%d、cutoffs=%s（train 稀有度換算）"
          % (FUNDING_PCT_WINDOW, FUNDING_PCT_LOW_CUTS))
    print("  ⚠️ 絕對階梯自己的 holdout AUC 也只有 0.485（n=13）＝這個子項兩版都沒訊號")


if __name__ == "__main__":
    main()
