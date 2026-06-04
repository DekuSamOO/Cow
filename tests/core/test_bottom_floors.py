"""
tests/core/test_bottom_floors.py
最低價綜合評估 — 離線單元測試（無網路依賴，注入 onchain/hashrate）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

from core import miner_cost
from core.season_forecast import extrapolate_bottom_mult
from core.bottom_floors import compute_all_bottom_estimates


# ── miner_cost ──────────────────────────────────────────────
def test_btc_per_day_halving_eras():
    assert miner_cost.btc_per_day(datetime(2013, 1, 1)) == 3600.0
    assert miner_cost.btc_per_day(datetime(2017, 1, 1)) == 1800.0
    assert miner_cost.btc_per_day(datetime(2021, 1, 1)) == 900.0
    assert miner_cost.btc_per_day(datetime(2025, 1, 1)) == 450.0


def test_eff_jth_monotonic_decreasing():
    ds = [datetime(y, 1, 1) for y in range(2014, 2026)]
    effs = [miner_cost.eff_jth(d) for d in ds]
    assert all(a >= b for a, b in zip(effs, effs[1:])), "效率應隨時間下降"


def test_allin_is_factor_of_elec():
    e = miner_cost.electricity_breakeven(2.5e8, datetime(2022, 11, 21))
    a = miner_cost.all_in_cost(2.5e8, datetime(2022, 11, 21))
    assert abs(a - e * miner_cost.ALLIN_FACTOR) < 1e-6
    assert e > 0


# ── bottom_mult 趨勢外插 ─────────────────────────────────────
def test_bottom_mult_trend_increasing():
    pts = [extrapolate_bottom_mult(i)[0] for i in range(4)]
    assert all(a < b for a, b in zip(pts, pts[1:])), "底部倍數應逐輪遞增（底部漸淺）"
    p, deep, shallow = extrapolate_bottom_mult(3)
    assert deep < p < shallow


# ── bottom_floors 整合 ───────────────────────────────────────
def _synthetic_df(days=1500, start_price=20000.0):
    idx = pd.date_range(end=datetime(2026, 6, 1), periods=days, freq="D")
    # 平滑上升路徑（確保 SMA200/730 與週線 SMA200 可算）
    close = np.linspace(start_price, start_price * 5, days)
    return pd.DataFrame({"close": close}, index=idx)


def test_compute_final_low_is_max_season_and_miner_elec():
    df = _synthetic_df()
    price = float(df["close"].iloc[-1])
    onchain = {"realized_price": 50000, "balanced_price": 30000, "cvdd": 15000, "asof": "2026-06-03"}
    res = compute_all_bottom_estimates(price, df=df, hashrate_ths=7.6e8,
                                       now=datetime(2026, 6, 1), onchain=onchain)
    season_mid = res["season_bottom"]["bottom_mid"]
    assert abs(res["final_low"] - max(season_mid, res["miner_elec"])) < 1e-6
    assert res["final_low_basis"] in ("礦工電費硬地板", "四季論趨勢底")


def test_estimates_filter_none_and_zero():
    df = _synthetic_df()
    price = float(df["close"].iloc[-1])
    # 不給 hashrate / onchain → 礦工 & on-chain 項應缺席，但技術 floor 仍在
    res = compute_all_bottom_estimates(price, df=df, hashrate_ths=None,
                                       now=datetime(2026, 6, 1), onchain=None)
    keys = {e["key"] for e in res["estimates"]}
    assert "miner_elec" not in keys and "realized" not in keys
    assert "power_law" in keys and "ma200w" in keys
    assert all(e["value"] > 0 for e in res["estimates"])


def test_ensemble_is_median_of_strong_anchors():
    import statistics
    df = _synthetic_df()
    price = float(df["close"].iloc[-1])
    onchain = {"realized_price": 50000, "balanced_price": 30000, "cvdd": 15000, "asof": "x"}
    res = compute_all_bottom_estimates(price, df=df, hashrate_ths=7.6e8,
                                       now=datetime(2026, 6, 1), onchain=onchain)
    s = res["season_bottom"]
    # 重建強錨集合
    by = {e["key"]: e["value"] for e in res["estimates"]}
    strong = [v for v in (s["bottom_mid"], by.get("ma200w"), res["miner_elec"],
                          50000, 30000, 15000) if v]
    assert abs(res["ensemble_low"] - statistics.median(strong)) < 1e-6


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
