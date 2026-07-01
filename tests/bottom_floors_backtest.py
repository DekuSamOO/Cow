"""
tests/bottom_floors_backtest.py
最低價綜合評估 — 回測驗證（4 節對齊優化 PLAN §4）
─────────────────────────────────────────────────────────────────────
A. 礦工成本重建 vs 三輪實際熊底（電費硬地板 / all-in 跌破）
B. bottom_mult 趨勢外插 vs 舊 median（留一法）
C. on-chain 錨（Realized/Balanced/CVDD）vs 2022-11-21 實際熊底
D. final_low 地板效果示意

執行：PYTHONIOENCODING=utf-8 python tests/bottom_floors_backtest.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime

from core.miner_cost import electricity_breakeven, all_in_cost, eff_jth, btc_per_day
from core.season_forecast import extrapolate_bottom_mult, STATS, _bottom_mults
from service.bottom_metrics import fetch_hashrate_history_ths, to_frame

BOTTOMS = [
    ("2015-01-14", 152.40),
    ("2018-12-15", 3122.0),
    ("2022-11-21", 15476.0),
]


def section_a():
    print("=== A. 礦工成本重建 vs 三輪實際熊底 ===")
    hr = fetch_hashrate_history_ths()
    if not hr:
        print("  （算力歷史抓取失敗，略過）"); return
    def near(ts):
        b = min(hr.keys(), key=lambda d: abs((datetime.fromisoformat(d) - datetime.fromisoformat(ts)).days))
        return hr[b]
    print(f"  {'熊底日':<12}{'熊底$':>9}{'算力EH':>8}{'eff':>5}{'電費$':>9}{'allin$':>9}{'底/電費':>8}{'底/allin':>9}")
    for d, low in BOTTOMS:
        dt = datetime.fromisoformat(d); h = near(d)
        e = electricity_breakeven(h, dt); a = all_in_cost(h, dt)
        print(f"  {d:<12}{low:>9,.0f}{h/1e6:>8.1f}{eff_jth(dt):>5.0f}{e:>9,.0f}{a:>9,.0f}{low/e:>7.2f}x{low/a:>8.2f}x")
    print("  → 熊底/電費 收斂且 >1（電費=硬地板）；2018/2022 底/allin<1（牛末跌破 allin，電價0.05 下 ≈0.73-0.76）\n")


def section_b():
    print("=== B. bottom_mult 趨勢外插 vs 舊 median（留一法）===")
    bm = _bottom_mults
    # 留一法：用前兩輪外插第三輪
    xs, ys = [0, 1], bm[:2]
    mx = sum(xs) / 2; my = sum(ys) / 2
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    pred_trend = slope * 2 + (my - slope * mx)
    print(f"  實際 idx2 bottom_mult = {bm[2]:.3f}")
    print(f"  趨勢外插預測          = {pred_trend:.3f}  誤差 {(pred_trend/bm[2]-1)*100:+.0f}%")
    print(f"  舊 median 預測        = {STATS['bottom_mult_median']:.3f}  誤差 {(STATS['bottom_mult_median']/bm[2]-1)*100:+.0f}%")
    p, d, s = extrapolate_bottom_mult(3)
    print(f"  → 第4輪(idx3)趨勢外插 bottom_mult = {p:.3f}（深 {d:.3f} / 淺 {s:.3f}）\n")


def section_c():
    print("=== C. on-chain 錨 vs 2022-11-21 實際熊底 $15,476 ===")
    actual = 15476.0
    for key, label in [("realized_price", "Realized Price"),
                       ("balanced_price", "Balanced Price"),
                       ("cvdd", "CVDD")]:
        df = to_frame(key)
        if df.empty:
            print(f"  {label:<16}（無資料）"); continue
        sub = df[(df.index >= "2022-11-01") & (df.index <= "2022-12-10")]
        if sub.empty:
            print(f"  {label:<16}（2022-11 區間無資料，最早 {df.index.min().date()}）"); continue
        v = float(sub[key].mean())
        print(f"  {label:<16}{v:>10,.0f}   實際熊底/錨 = {actual/v:.2f}x")
    print("  → 實際熊底通常貼著/略低於 Realized、貼著 Balanced；CVDD 為更深絕對底\n")


def section_d():
    print("=== D. final_low 地板效果（現況示意）===")
    from service.bottom_metrics import get_latest_bottom_metrics
    from core.bottom_floors import compute_all_bottom_estimates
    from service.market_data import fetch_market_data
    btc, _ = fetch_market_data()
    price = float(btc["close"].iloc[-1])
    hr = fetch_hashrate_history_ths()
    h = hr[max(hr)] if hr else None
    res = compute_all_bottom_estimates(price, df=btc, hashrate_ths=h,
                                       onchain=get_latest_bottom_metrics())
    s = res["season_bottom"]
    print(f"  四季論趨勢底（未地板）= ${s['bottom_mid']:,.0f}" if s else "  四季論底: N/A")
    print(f"  礦工電費硬地板        = ${res['miner_elec']:,.0f}" if res['miner_elec'] else "")
    print(f"  → final_low = ${res['final_low']:,.0f}（依據 {res['final_low_basis']}）")
    print(f"  → ensemble  = ${res['ensemble_low']:,.0f}")


if __name__ == "__main__":
    section_a()
    section_b()
    section_c()
    section_d()
