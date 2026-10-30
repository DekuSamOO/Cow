"""scripts/move_attribution.py 純計算函式單元測試（不碰網路）。

背景：2026-10-30 凌晨 BTC −6%，待庵手算出「全市場同步跌、BNB 04:30~04:45 多跌一截」，
該套判讀固定成 move_attribution.py。這裡守住三件事：峰谷事件找對、起動時間不被前段緩跌拖偏、
classify 的判定分支各自走得到（用各分支專屬字串斷言，見 rules/testing.md No.23 ②）。
"""
import math

from scripts.move_attribution import (beta_and_resid_std, classify, find_event,
                                      hourly_returns, onset_index)


def _stat(ret, beta=1.0, resid_z=0.0, onset=0.0):
    return {"ret": ret, "beta": beta, "resid": 0.0, "resid_z": resid_z, "onset_min": onset}


def test_find_event_picks_largest_drawdown():
    closes = [100, 101, 102, 99, 95, 96, 97]
    i0, i1, pct = find_event(closes)
    assert (i0, i1) == (2, 4)
    assert pct < -6


def test_find_event_picks_rally_when_larger():
    closes = [100, 99, 98, 104, 108, 107]
    i0, i1, pct = find_event(closes)
    assert (i0, i1) == (2, 4)
    assert pct > 10


def test_find_event_never_single_bar():
    # 舊版用高低價：一根插到 94 又收回 100 的閃崩被判成 i0==i1 的 +6.7% 漲
    closes = [100, 100.2, 100.1, 99.8, 99.9]
    i0, i1, pct = find_event(closes)
    assert i0 < i1 and pct < 0


def test_find_event_v_shape_prefers_drop():
    # 100→95→100：跌 −5% 與漲 +5.26% 的對數幅度相同，不可因簡單 % 偏大就挑反彈
    i0, i1, pct = find_event([100, 95, 100.0])
    assert (i0, i1) == (0, 1) and pct < 0


def test_hourly_returns_skip_gaps():
    h = 3_600_000
    s = {0: 100.0, h: 101.0, 3 * h: 90.0, 4 * h: 91.0}
    r = hourly_returns(s)
    assert set(r) == {h, 4 * h}         # 2h→3h 缺一根，不可跨接成單根報酬


def test_onset_ignores_slow_drift():
    # 前 8 根緩跌各 −0.4%，第 9 根才是 −3% 的急跌；累積 30% 法會判成第 6 根
    closes = [100.0]
    for _ in range(8):
        closes.append(closes[-1] * 0.996)
    closes.append(closes[-1] * 0.97)
    closes.append(closes[-1] * 0.99)
    assert onset_index(closes, 0, len(closes) - 1, bar_sigma=0.004) == 9


def test_onset_falls_back_to_largest_bar():
    closes = [100, 99.9, 99.5, 99.4]
    assert onset_index(closes, 0, 3, bar_sigma=1.0) == 2


def test_beta_recovers_slope():
    x = {i: math.sin(i) * 0.01 for i in range(200)}
    y = {i: 1.5 * v for i, v in x.items()}
    b, sd = beta_and_resid_std(y, x)
    assert abs(b - 1.5) < 1e-9 and sd < 1e-9


def test_beta_aligns_by_timestamp():
    # y 少了前 10 根：若靠位置對齊（取尾端 n 根）會錯位，斜率不會是 1.5
    x = {i: math.sin(i) * 0.01 for i in range(200)}
    y = {i: 1.5 * x[i] for i in range(10, 200)}
    x_shifted = dict(x)
    for i in range(200, 210):
        x_shifted[i] = 0.05
    b, _ = beta_and_resid_std(y, x_shifted)
    assert abs(b - 1.5) < 1e-9


def test_classify_market_wide():
    stats = {"BTCUSDT": _stat(-0.05),
             "ETHUSDT": _stat(-0.06, 1.2), "SOLUSDT": _stat(-0.07, 1.4, onset=15)}
    r = classify(stats)
    assert r["verdict"].startswith("全市場")
    assert r["confidence"] == "高"


def test_classify_btc_specific():
    stats = {"BTCUSDT": _stat(-0.05),
             "ETHUSDT": _stat(0.01, 1.2), "SOLUSDT": _stat(-0.005, 1.4)}
    assert classify(stats)["verdict"] == "BTC 個別"


def test_classify_unsynced_is_mixed():
    stats = {"BTCUSDT": _stat(-0.05, onset=0),
             "ETHUSDT": _stat(-0.06, 1.2, onset=120), "SOLUSDT": _stat(-0.07, 1.4, onset=0)}
    assert classify(stats)["verdict"].startswith("混合")


def test_classify_flags_outlier_and_overlays():
    stats = {"BTCUSDT": _stat(-0.05),
             "ETHUSDT": _stat(-0.06, 1.2), "BNBUSDT": _stat(-0.08, 0.9, resid_z=-4.9)}
    r = classify(stats, premium_shift_pct=-0.5, oi_chg_pct=-4.0,
                 macro_hits=["FOMC 2026-10-28"], missing=["資金費率"])
    joined = "｜".join(r["layers"])
    assert r["outliers"] == ["BNBUSDT"]
    assert "個別幣多跌一截：BNBUSDT" in joined
    assert "交易所特定" in joined
    assert "槓桿清算成分" in joined
    assert "時點重疊：FOMC" in joined
    assert r["confidence"] == "中"


def test_classify_no_overlay_below_thresholds():
    stats = {"BTCUSDT": _stat(-0.05), "ETHUSDT": _stat(-0.06, 1.2, resid_z=-1.5)}
    r = classify(stats, premium_shift_pct=0.14, oi_chg_pct=-1.8)
    assert r["layers"] == [] and r["outliers"] == []
