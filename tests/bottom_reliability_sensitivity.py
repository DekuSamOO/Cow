"""
tests/bottom_reliability_sensitivity.py
BOTTOM_RELIABILITY 權重敏感度 — config.BOTTOM_RELIABILITY 的 10 個可靠度權重（主觀設定）
對 ensemble_low（強錨加權中位數）的影響有多大？

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/bottom_reliability_sensitivity.py

方法：
  以現況跑 compute_all_bottom_estimates 取 baseline ensemble_low，再
  (1) leave-one-out：逐一把某錨權重設 0（移除），看 ensemble_low 位移；
  (2) 全域擾動：所有權重 ×0.5 / ×1.5，看位移。
  ensemble 為「加權中位數」→ 預期對單一權重不敏感（中位數穩健）。位移小 = 權重設定不必精算。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import copy
import numpy as np
import pandas as pd

from service.market_data import fetch_market_data
from service.bottom_metrics import get_latest_bottom_metrics
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators
import core.bottom_floors as bf

_BASE = copy.deepcopy(bf._RELIABILITY)


def _ensemble(price, df, onchain):
    res = bf.compute_all_bottom_estimates(price, df=df, hashrate_ths=None, onchain=onchain)
    return res.get("ensemble_low"), res.get("final_low"), res.get("final_low_basis")


def main():
    print("載入資料 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)
    price = float(btc.iloc[-1]["close"])
    try:
        onchain = get_latest_bottom_metrics()
    except Exception as e:
        print("onchain 取得失敗，用 None：", e); onchain = None

    bf._RELIABILITY = copy.deepcopy(_BASE)
    base_ens, base_fl, basis = _ensemble(price, btc, onchain)
    if base_ens is None:
        print("ensemble_low 取不到，無法做敏感度。"); return
    print(f"\nbaseline：現價 ${price:,.0f}｜ensemble_low ${base_ens:,.0f}｜"
          f"final_low ${base_fl:,.0f}（{basis}）")

    print("\n=== leave-one-out：移除單一錨權重 → ensemble_low 位移 ===")
    rows = []
    for k in _BASE:
        bf._RELIABILITY = copy.deepcopy(_BASE); bf._RELIABILITY[k] = 0
        ens, _, _ = _ensemble(price, btc, onchain)
        if ens:
            shift = (ens / base_ens - 1) * 100
            rows.append((k, ens, shift))
    for k, ens, shift in sorted(rows, key=lambda x: -abs(x[2])):
        flag = "  ← 主導" if abs(shift) >= 3 else ""
        print(f"  移除 {k:14s} ensemble ${ens:,.0f}  位移 {shift:+5.1f}%{flag}")

    print("\n=== 全域擾動：所有可靠度 ×factor ===")
    for fac in (0.5, 1.5):
        bf._RELIABILITY = {k: v * fac for k, v in _BASE.items()}
        ens, _, _ = _ensemble(price, btc, onchain)
        if ens:
            print(f"  ×{fac}: ensemble ${ens:,.0f}  位移 {(ens/base_ens-1)*100:+.1f}%")

    bf._RELIABILITY = copy.deepcopy(_BASE)
    max_shift = max((abs(s) for _, _, s in rows), default=0)
    print("\n結論：", end="")
    if max_shift < 3:
        print(f"ensemble_low 對 reliability 權重**高度穩健**（最大 leave-one-out 位移 {max_shift:.1f}%）"
              "→ 權重採主觀設定即可，毋須精算。加權中位數本質使單一權重影響有限。")
    else:
        print(f"有錨主導 ensemble（最大位移 {max_shift:.1f}%）→ 該錨權重值得用三輪熊底回測校準。")


if __name__ == "__main__":
    main()
