"""
tests/relative_ref_signals_backtest.py
社群參考訊號（MVRV-Z 逃頂/抄底、Hash Ribbons 抄底）敏感度驗證 — 手動執行（非 pytest）。

  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/relative_ref_signals_backtest.py

方法（鏡像 tests/relative_high_backtest.py / relative_low_backtest.py 已拍板的方法論）：
  1. 標記相對高點/低點：日線 swing high/low(order=10)，其後 60 天內回撤/反彈 ≥18% 為正樣本。
  2. MVRV-Z、Hash Ribbons 皆用**本地已快取歷史**（零新網路請求）：
       db/bottom_metrics_cache.json  → mvrv_zscore（2022-07+ 逐日）
       db/hashrate_history.json      → 全歷史算力（2009+，Hash Ribbons 用 SMA30/60 交叉）
     樣本僅取落在各自資料涵蓋期內者（MVRV-Z 2022-07+；Hash Ribbons 需 ≥60 日前置歷史）。
  3. AUC 用 Mann-Whitney U（與逃頂/抄底回測同實作）；方向已轉換成「越高越像頂/底」的
     單調子分數，AUC>0.5 代表方向正確，≥0.55 為專案既有採用門檻。
  4. 結論寫回 core/relative_high.py / relative_low.py 的 reference_*_signals 與
     BTC_WATCH.py 的 _ref_rows 標籤文字（誠實反映「已驗證」或「已驗證仍弱維持參考」，
     不再籠統寫「待回測」）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from service.market_data import fetch_market_data

DROP_THRESH = -0.18
RALLY_THRESH = 0.18
HORIZON = 60
ORDER = 10
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def auc(pos, neg):
    """Mann-Whitney U based AUC，與 relative_high/low_backtest.py 同實作。"""
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


def label_swings(btc, kind):
    """kind='top' 或 'bottom'。回傳 (pos_idx, neg_idx)，鏡像既有兩支回測腳本的標記法。"""
    high = btc["high"].values.astype(float)
    low = btc["low"].values.astype(float)
    close = btc["close"].values.astype(float)
    n = len(btc)
    pos = []
    for i in range(ORDER, n - ORDER):
        if kind == "top":
            w = high[i - ORDER:i + ORDER + 1]
            is_extreme = high[i] == np.nanmax(w) and (w == high[i]).sum() == 1
        else:
            w = low[i - ORDER:i + ORDER + 1]
            is_extreme = low[i] == np.nanmin(w) and (w == low[i]).sum() == 1
        if not is_extreme:
            continue
        fut = close[i + 1:i + HORIZON + 1]
        if not len(fut):
            continue
        if kind == "top" and (fut.min() / close[i] - 1) <= DROP_THRESH:
            pos.append(i)
        elif kind == "bottom" and (fut.max() / close[i] - 1) >= RALLY_THRESH:
            pos.append(i)
    rng = np.random.default_rng(42)
    era_idx = [k for k in range(ORDER, n - ORDER)]
    pos_set = set(pos)
    def far(k):
        return all(abs(k - p) > 45 for p in pos)
    neg_pool = [k for k in era_idx if k not in pos_set and far(k)]
    neg = list(rng.choice(neg_pool, size=min(len(pos) * 2, len(neg_pool)), replace=False)) if pos and neg_pool else []
    return pos, neg


def load_mvrv_map():
    p = os.path.join(_ROOT, "db", "bottom_metrics_cache.json")
    d = json.load(open(p, encoding="utf-8"))
    return {k: float(v) for k, v in (d.get("mvrv_zscore") or {}).items()}


def load_hashrate_series():
    p = os.path.join(_ROOT, "db", "hashrate_history.json")
    d = json.load(open(p, encoding="utf-8"))
    raw = d.get("data") or {}
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in raw.items()}).sort_index()
    return s


def report(name, pos_vals, neg_vals):
    a = auc(pos_vals, neg_vals)
    n_pos = sum(1 for v in pos_vals if v is not None and not np.isnan(v))
    n_neg = sum(1 for v in neg_vals if v is not None and not np.isnan(v))
    verdict = "✅ 過門檻(≥0.55)" if a >= 0.55 else ("⚠️ 弱(0.5~0.55)" if a >= 0.5 else "❌ 方向反/無效")
    print(f"[{name}] n_pos={n_pos} n_neg={n_neg}  AUC={a:.3f}  {verdict}")
    return a


def main():
    print("抓 BTC 日線（本地快取優先，零新網路請求風險）…")
    btc, _ = fetch_market_data()
    print(f"BTC 日線：n={len(btc)}  {btc.index[0].date()} ~ {btc.index[-1].date()}")

    mvrv_map = load_mvrv_map()
    mvrv_start = pd.Timestamp(min(mvrv_map)) if mvrv_map else None
    print(f"MVRV-Z 快取：n={len(mvrv_map)}  {min(mvrv_map)} ~ {max(mvrv_map)}" if mvrv_map else "MVRV-Z 快取為空")

    hr = load_hashrate_series()
    print(f"算力快取：n={len(hr)}  {hr.index[0].date()} ~ {hr.index[-1].date()}")
    sma30, sma60 = hr.rolling(30).mean(), hr.rolling(60).mean()
    # 「捕捉強度」= (SMA60-SMA30)/SMA60，正值代表 SMA30<SMA60（投降中），值越大投降越深
    hr_capitulation = ((sma60 - sma30) / sma60).dropna()

    print("\n" + "=" * 72)
    print("【逃頂側】MVRV-Z：值越高越像頂（AUC>0.5 = 方向正確）")
    print("=" * 72)
    pos_i, neg_i = label_swings(btc, "top")
    print(f"相對高點 {len(pos_i)} 個（order=10, 60日內回撤≥18%）；負樣本 {len(neg_i)} 個")
    def mvrv_at(k):
        ds = btc.index[k].strftime("%Y-%m-%d")
        return mvrv_map.get(ds) if (mvrv_start and btc.index[k] >= mvrv_start) else None
    pos_top = [mvrv_at(k) for k in pos_i]
    neg_top = [mvrv_at(k) for k in neg_i]
    n_cover = sum(1 for v in pos_top if v is not None) + sum(1 for v in neg_top if v is not None)
    print(f"MVRV-Z 時代內樣本數：{n_cover}（其餘因日期早於 2022-07 快取起點被跳過）")
    report("逃頂-MVRV-Z", pos_top, neg_top)

    print("\n" + "=" * 72)
    print("【抄底側】MVRV-Z：值越低越像底 → 用 -z 當單調子分數（AUC>0.5 = 方向正確）")
    print("=" * 72)
    pos_i, neg_i = label_swings(btc, "bottom")
    print(f"相對低點 {len(pos_i)} 個（order=10, 60日內反彈≥18%）；負樣本 {len(neg_i)} 個")
    pos_bot_mvrv = [(-v if v is not None else None) for v in (mvrv_at(k) for k in pos_i)]
    neg_bot_mvrv = [(-v if v is not None else None) for v in (mvrv_at(k) for k in neg_i)]
    n_cover = sum(1 for v in pos_bot_mvrv if v is not None) + sum(1 for v in neg_bot_mvrv if v is not None)
    print(f"MVRV-Z 時代內樣本數：{n_cover}")
    report("抄底-MVRV-Z", pos_bot_mvrv, neg_bot_mvrv)

    print("\n" + "=" * 72)
    print("【抄底側】Hash Ribbons：礦工投降強度 (SMA60-SMA30)/SMA60，越大越像底")
    print("=" * 72)
    def hr_at(k):
        d = btc.index[k]
        if d not in hr_capitulation.index:
            # 對齊到最近可用日（算力資料源與 BTC 日線交易日曆不完全一致）
            idx = hr_capitulation.index.searchsorted(d)
            if idx >= len(hr_capitulation):
                return None
            near = hr_capitulation.index[idx]
            if abs((near - d).days) > 3:
                return None
            return float(hr_capitulation.iloc[idx])
        return float(hr_capitulation.loc[d])
    pos_hr = [hr_at(k) for k in pos_i]
    neg_hr = [hr_at(k) for k in neg_i]
    n_cover = sum(1 for v in pos_hr if v is not None) + sum(1 for v in neg_hr if v is not None)
    print(f"算力資料涵蓋樣本數：{n_cover}")
    report("抄底-HashRibbons", pos_hr, neg_hr)

    print("\n完成。AUC≥0.55 建議轉正式權重（需另外決定配重數字，比照既有 v0.2/v0.3 方法論）；")
    print("0.5~0.55 維持參考但可標「已驗證弱」；<0.5 代表方向可能有問題，需再檢視。")


if __name__ == "__main__":
    main()
