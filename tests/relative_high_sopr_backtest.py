"""
tests/relative_high_sopr_backtest.py
逃頂雷達 onchain 的 SOPR 子項驗證 — 鏡像 tests/relative_low_backtest.validate_unfitted_dims（抄底側）。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/relative_high_sopr_backtest.py

背景：抄底側 SOPR（割肉 ≤0.95）2026-06 已驗（AUC 0.585）並移出 UNFITTED。逃頂側 SOPR
（獲利了結飆高 ≥1.05/1.08）尚未鏡像驗證，relative_high.UNFITTED_DIMS 仍含 onchain。

方法（與抄底側對稱）：
  標頂 = swing high(order=10) + 其後60日回撤≥18%（正樣本）；非頂 = 遠離頂的隨機日（負樣本）。
  SOPR 期內樣本，用真正的 compute_escape_top_score 餵歷史 SOPR：
    (1) onchain 子分(僅 SOPR，etf=None→0) 單維 AUC（方向：頂部 SOPR 子分應較高）
    (2) 合成「加 onchain vs 不加」AUC（增量無害/有益）
    (3) onchain 權重擾動穩健性
  判定：AUC>0.55 且增量無害 → 可移出 UNFITTED（標逃頂 SOPR 驗證）；否則維持灰燈、不重訂門檻。

⚠️ 限制：SOPR 歷史僅 2022-06+（db/bottom_metrics_cache.json），頂部樣本少 → 信度有限。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import json
import numpy as np
import pandas as pd
import requests, urllib3
urllib3.disable_warnings()

from service.market_data import fetch_market_data
from service.onchain import fetch_aux_history
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators
from core.relative_high import compute_escape_top_score, annualize_funding

ORDER = 10
HORIZON = 60
DRAW = 0.18


def auc(pos, neg):
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    a = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    a.sort(key=lambda x: x[0])
    r = {}; i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[j + 1][0] == a[i][0]:
            j += 1
        rr = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[k] = rr
        i = j + 1
    rs = sum(r[k] for k, (v, l) in enumerate(a) if l == 1)
    return (rs - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _load_sopr():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "db", "bottom_metrics_cache.json")
    try:
        c = json.load(open(p, encoding="utf-8"))
        return {d: float(v) for d, v in (c.get("sopr") or {}).items()}
    except Exception as e:
        print("SOPR 快取讀取失敗：", e); return {}


def main():
    print("載入資料 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)

    _, _, fund = fetch_aux_history()
    fund_daily = pd.Series(dtype=float)
    if fund is not None and not fund.empty and "fundingRate" in fund.columns:
        f = fund.copy()
        if f.index.tz is not None:
            f.index = f.index.tz_localize(None)
        fund_daily = f["fundingRate"].resample("D").mean()
    fng_map = {}
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=0&format=json", timeout=20, verify=False)
        for it in r.json().get("data", []):
            fng_map[pd.to_datetime(int(it["timestamp"]), unit="s").strftime("%Y-%m-%d")] = float(it["value"])
    except Exception as e:
        print("F&G 抓取失敗：", e)

    sopr_map = _load_sopr()
    if not sopr_map:
        print("無 SOPR 快取 → 無法驗證，維持 UNFITTED。"); return
    sopr_start = pd.Timestamp(min(sopr_map))
    print(f"SOPR 歷史 n={len(sopr_map)}，{min(sopr_map)} ~ {max(sopr_map)}")

    high = btc["high"].values.astype(float)
    close = btc["close"].values.astype(float)
    n = len(btc)
    tops = []
    for i in range(ORDER, n - ORDER):
        w = high[i - ORDER:i + ORDER + 1]
        if high[i] == np.nanmax(w) and (w == high[i]).sum() == 1:
            fut = close[i + 1:i + HORIZON + 1]
            if len(fut) and (fut.min() / close[i] - 1) <= -DRAW:
                tops.append(i)
    rng = np.random.default_rng(0)
    nontop = [k for k in range(ORDER, n - ORDER) if all(abs(k - t) > 30 for t in tops)]
    nt = list(rng.choice(nontop, size=min(len(tops) * 3, len(nontop)), replace=False))

    def sample(k):
        d = btc.index[k]; ds = d.strftime("%Y-%m-%d")
        f8 = None
        if not fund_daily.empty:
            v = float(fund_daily.get(d.normalize(), np.nan)); f8 = None if np.isnan(v) else v
        in_s = d >= sopr_start
        sopr_val = sopr_map.get(ds) if in_s else None
        sc, sig = compute_escape_top_score(btc.iloc[k], btc.iloc[:k + 1],
                                           funding_8h=f8, fng=fng_map.get(ds), sopr=sopr_val)
        return sc, sig["onchain"]["score"], in_s

    pos = [sample(k) for k in tops]
    neg = [sample(k) for k in nt]
    pos_s = [(sc, oc) for sc, oc, ok in pos if ok]
    neg_s = [(sc, oc) for sc, oc, ok in neg if ok]
    print(f"SOPR 期內樣本：頂 {len(pos_s)} / 非頂 {len(neg_s)}")
    if len(pos_s) < 4 or len(neg_s) < 4:
        print("SOPR 期內頂部樣本過少（<4）→ 信度不足，維持 UNFITTED、不重訂門檻。"); return

    oc_auc = auc([o for _, o in pos_s], [o for _, o in neg_s])
    print(f"\n[onchain 方向] SOPR 子分 頂均 {np.mean([o for _,o in pos_s]):.2f} / "
          f"非頂均 {np.mean([o for _,o in neg_s]):.2f}  AUC={oc_auc:.3f}（>0.5＝頂部 SOPR 子分較高）")
    with_auc = auc([sc for sc, _ in pos_s], [sc for sc, _ in neg_s])
    wo_auc = auc([sc - o for sc, o in pos_s], [sc - o for sc, o in neg_s])
    print(f"[onchain 增量] 合成 AUC：加 onchain {with_auc:.3f} vs 不加 {wo_auc:.3f}  "
          f"Δ={with_auc - wo_auc:+.3f}")
    print("[onchain 擾動] onchain 權重 ×factor 下合成 AUC：")
    for fac in (0.0, 0.5, 1.0, 1.5):
        a = auc([sc - o + o * fac for sc, o in pos_s], [sc - o + o * fac for sc, o in neg_s])
        print(f"    ×{fac:<3} AUC={a:.3f}")

    ok = (oc_auc > 0.55) and (with_auc - wo_auc >= -0.005)
    print("\n" + "-" * 60)
    print(f"結論：逃頂 SOPR 方向 {'正確' if oc_auc > 0.5 else '不正確'}（AUC {oc_auc:.3f}）、"
          f"增量 {'無害' if with_auc - wo_auc >= -0.005 else '有害'}（Δ {with_auc - wo_auc:+.3f}）→ "
          + ("AUC>0.55 且無害 → 可移出 UNFITTED（標逃頂 SOPR 驗證）。"
             if ok else "未達 AUC>0.55 或樣本薄 → **維持 UNFITTED、不重訂門檻**（待 2028 更多週期）。"))


if __name__ == "__main__":
    main()
