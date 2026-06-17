"""
tests/position_calib.py
倉位區間擬合 — 用 core/radar_replay 回放三軸分數，檢驗 core/action_ensemble 各分支的
「建議倉位區間」是否與歷史實證報酬一致，並據可擬合者反推倉位。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/position_calib.py

方法：
  1. 逐日回放 逃頂/抄底/趨勢 分數（radar_replay）。
  2. 每日呼叫 action_ensemble.compute_composite_action(trend, escape, low) 取 action_key（單一來源）。
  3. 各 action_key 分支 → 其後 60 日「期末報酬 / 最大回撤」分布、樣本數。
  4. 另以「趨勢淨分桶 → 其後60日期末報酬」直接驗證倉位應隨趨勢遞增。
  5. 據實證反推可擬合分支的倉位區間（保守下界，打折）。

⚠️ 重大限制（與 alert_threshold_calib 同源）：
  回放逃頂/抄底分為保守下界（OI/ETF/SOPR=0，天花板 ~55）→ ESCAPE_HOT=60 / LOW_STRONG=75 等
  分支在回放中幾乎不觸發、樣本不足，**無法擬合**；可擬合者僅 trend 主導的中性估值分支
  （RIDE / DEFENSE / RANGE 等）。estimation-gated 分支維持專家設定。
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
from core.radar_replay import escape_score_series, low_score_series, trend_score_series
from core.action_ensemble import compute_composite_action

HORIZON = 60


def _load():
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
        r = requests.get("https://api.alternative.me/fng/?limit=0&format=json",
                         timeout=20, verify=False)
        for it in r.json().get("data", []):
            d = pd.to_datetime(int(it["timestamp"]), unit="s").strftime("%Y-%m-%d")
            fng_map[d] = float(it["value"])
    except Exception as e:
        print("F&G 歷史抓取失敗：", e)
    return btc, fund_daily, fng_map


def _fwd(close, dates_idx, i):
    """第 i 天其後 60 日（期末報酬, 最大回撤, 最大漲幅）。"""
    c = close.values.astype(float)
    fut = c[i + 1:i + 1 + HORIZON]
    if not len(fut) or np.isnan(c[i]):
        return None
    return fut[-1] / c[i] - 1, fut.min() / c[i] - 1, fut.max() / c[i] - 1


def main():
    print("載入資料 …")
    btc, fund_daily, fng_map = _load()
    close = btc["close"]
    escape = escape_score_series(btc, fund_daily, fng_map)
    low = low_score_series(btc, fund_daily, fng_map)
    trend = trend_score_series(btc)

    idx = escape.dropna().index.intersection(trend.dropna().index)
    pos_of_i = {d: k for k, d in enumerate(close.index)}

    # ── 1) 各 action_key 分支實證 ────────────────────────────────────────────
    rows = {}
    for d in idx:
        a = compute_composite_action(
            float(trend.get(d)) if d in trend.index else None,
            float(escape.get(d)) if d in escape.index else None,
            float(low.get(d)) if d in low.index else None)
        if a is None:
            continue
        f = _fwd(close, None, pos_of_i[d])
        if f is None:
            continue
        rows.setdefault(a["action_key"], {"end": [], "dd": [], "pos": (a["pos_low"], a["pos_high"])})
        rows[a["action_key"]]["end"].append(f[0])
        rows[a["action_key"]]["dd"].append(f[1])

    print("\n=== 各 action_key 分支 → 其後60日 期末報酬/最大回撤（回放）===")
    print(f"{'action_key':16s} {'n':>4s} {'現倉位':>8s} {'報酬中位':>8s} {'報酬均':>7s} "
          f"{'回撤中位':>8s} {'勝率':>5s}")
    order = ["ADD", "RIDE", "HOLD_TIGHTEN", "TAKE_PROFIT", "ACCUMULATE", "RANGE", "REDUCE",
             "BOTTOM_FISH", "WATCH_REVERSAL", "FADE_RALLY", "DEFENSE"]
    fittable = []
    for k in order:
        if k not in rows:
            continue
        e = np.array(rows[k]["end"]); dd = np.array(rows[k]["dd"])
        n = len(e); plo, phi = rows[k]["pos"]
        win = (e > 0).mean() * 100
        mark = "" if n >= 20 else "  ← 樣本不足"
        if n >= 20:
            fittable.append((k, np.median(e), np.median(dd), plo, phi))
        print(f"{k:16s} {n:4d} {plo:3d}-{phi:<3d}% {np.median(e)*100:+7.1f}% {np.mean(e)*100:+6.1f}% "
              f"{np.median(dd)*100:+7.1f}% {win:4.0f}%{mark}")

    # ── 2) 趨勢淨分桶 → 其後60日期末報酬（倉位應隨趨勢遞增的直接證據）──────────
    print("\n=== 趨勢淨分桶 → 其後60日期末報酬（倉位主軸驗證）===")
    tv = trend.reindex(idx).values
    ev = np.array([_fwd(close, None, pos_of_i[d]) for d in idx], dtype=object)
    end = np.array([x[0] if x else np.nan for x in ev])
    for lo, hi, lbl in [(-101, -50, "強空 ≤-50"), (-50, -20, "空 -50~-20"),
                        (-20, 20, "中性 -20~20"), (20, 50, "多 20~50"), (50, 101, "強多 ≥50")]:
        m = (tv >= lo) & (tv < hi) & ~np.isnan(end)
        if m.sum() > 10:
            print(f"  {lbl:14s} n={m.sum():4d}  報酬中位 {np.median(end[m])*100:+6.1f}%  "
                  f"勝率 {(end[m]>0).mean()*100:3.0f}%")

    print("\n" + "=" * 72)
    print("判讀：")
    print("  - 樣本≥20 的分支才可擬合；estimation-gated 分支(逃頂≥60/抄底≥75)回放幾乎不觸發→維持專家設定。")
    print("  - 趨勢分桶若『報酬中位 隨趨勢遞增』→ 證實倉位應隨趨勢主軸遞增（現行階梯方向正確）。")
    print("  - 回放為保守下界 → 反推倉位打 0.85 折或維持專家上緣，避免高估。")


if __name__ == "__main__":
    main()
