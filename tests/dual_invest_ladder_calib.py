"""
tests/dual_invest_ladder_calib.py
雙幣梯形權重「資料半」— 各檔（激進/中性/保守）歷史行權率 + 權利金(APY) 證據。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/dual_invest_ladder_calib.py

背景：strategy/dual_invest.calculate_ladder_strategy 產 3 檔行權價，現行資金權重
  激進 30% / 中性 30% / 保守 40%（硬訂）。本腳本回測各檔：
    - 行權率（t_days 內價格觸及行權價的比例）：SELL_HIGH 看其後高點≥strike；BUY_LOW 看低點≤strike。
    - 權利金 APY 中位（calculate_ladder_strategy 內 BS 估）。
  → 提供「行權率 × 權利金」的實證權衡。

⚠️ 最終權重是**風險偏好選擇**（多收 premium=偏激進 / 少被行權=偏保守），非純資料可定；
   本腳本只給數據，權重由使用者偏好定。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from service.market_data import fetch_market_data
from core.indicators import calculate_technical_indicators
from strategy.dual_invest import calculate_ladder_strategy

T_DAYS = 3
TIERS = ["激進", "中性", "保守"]


def _parse_apy(s):
    try:
        return float(str(s).replace("%", ""))
    except Exception:
        return np.nan


def _backtest(btc, product_type):
    high = btc["high"].values.astype(float)
    low = btc["low"].values.astype(float)
    n = len(btc)
    acc = {t: {"exer": [], "apy": [], "dist": []} for t in TIERS}
    need = ["ATR", "close", "BB_Upper", "BB_Lower"]
    for k in range(60, n - T_DAYS):
        row = btc.iloc[k]
        if any(c not in row or pd.isna(row[c]) for c in need):
            continue
        try:
            targets = calculate_ladder_strategy(row, product_type, t_days=T_DAYS)
        except Exception:
            continue
        fut_hi = high[k + 1:k + 1 + T_DAYS]
        fut_lo = low[k + 1:k + 1 + T_DAYS]
        if not len(fut_hi):
            continue
        for tgt in targets:
            t = tgt["Type"]
            strike = float(tgt["Strike"])
            exercised = (fut_hi.max() >= strike) if product_type == "SELL_HIGH" else (fut_lo.min() <= strike)
            acc[t]["exer"].append(1 if exercised else 0)
            acc[t]["apy"].append(_parse_apy(tgt["APY(年化)"]))
            acc[t]["dist"].append(float(tgt["Distance"]))
    return acc


def _report(name, acc):
    print(f"\n=== {name} 各檔回測（t_days={T_DAYS}）===")
    print(f"{'檔位':6s} {'n':>5s} {'行權率':>6s} {'權利金APY中位':>12s} {'距現價中位':>9s} {'現權重':>6s}")
    weights = {"激進": "30%", "中性": "30%", "保守": "40%"}
    for t in TIERS:
        a = acc[t]
        if not a["exer"]:
            continue
        print(f"{t:6s} {len(a['exer']):5d} {np.mean(a['exer'])*100:5.0f}% "
              f"{np.nanmedian(a['apy']):11.1f}% {np.nanmedian(a['dist']):8.1f}% {weights[t]:>6s}")


def main():
    print("載入資料 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)

    sell = _backtest(btc, "SELL_HIGH")
    buy = _backtest(btc, "BUY_LOW")
    _report("SELL_HIGH（賣高/賣call）", sell)
    _report("BUY_LOW（買低/賣put）", buy)

    print("\n" + "=" * 64)
    print("判讀：")
    print("  - 激進(近檔)：行權率高、權利金高 → 常被行權（SELL_HIGH 常賣出/BUY_LOW 常買入），")
    print("    換取較高 premium；保守(遠檔)：行權率低、權利金低 → 少被行權、保留部位。")
    print("  - 現行 30/30/40 把較多資金放保守檔＝偏『少被行權、保部位』的風險趨避預設。")
    print("  - 最終權重屬風險偏好：偏積極收 premium → 加重激進；偏保守保部位 → 維持/加重保守。")
    print("    本腳本給行權率×權利金的量級，權重由偏好定（非純資料最優）。")


if __name__ == "__main__":
    main()
