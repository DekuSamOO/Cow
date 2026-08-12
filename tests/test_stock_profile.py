"""scripts/stock_profile.py 純函數測試（合成資料、零網路、零 DB）。

只測「會靜默給出錯誤數字」的那幾件事：金額量級、還原基準一致性、成交額分位、
分級門檻邊界。網路與 climber DB 那層由呼叫端自然重試，不在此模擬。
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from stock_profile import _fmt_money, short_term_traits, _margin_chg   # noqa: E402


def _df(n=300, close=100.0, vol=1_000_000, spread=0.02):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    c = pd.Series([close] * n, index=idx, dtype=float)
    return pd.DataFrame({
        "open": c, "high": c * (1 + spread), "low": c * (1 - spread),
        "close": c, "volume": [float(vol)] * n,
    }, index=idx)


# ── 金額量級（會直接讓人讀錯 3 個數量級的那種錯）─────────────────────────────

def test_fmt_money_has_trillion_tier_for_twd():
    """2330 市值 6.2e13，只到「億」會印成 621,080 億元——量級瞬間讀不出來。"""
    assert _fmt_money(6.21e13, "TWD") == "62.10 兆元"
    assert _fmt_money(1.3356e10, "TWD") == "133.56 億元"
    assert _fmt_money(6.81e7, "TWD") == "6,810 萬元"
    assert _fmt_money(None, "TWD") == "—"


def test_fmt_money_usd_scales_to_billions():
    assert _fmt_money(2.629e10, "USD") == "$26.29B"
    assert _fmt_money(5.5e6, "USD") == "$5.5M"


# ── 短線特性 ────────────────────────────────────────────────────────────────

def test_turnover_pctile_uses_dollars_not_shares():
    """跨年代比較必須用成交額：股價長期上漲時，股數會萎縮而金額成長。
    NVDA 實例：股數分位 0、成交額分位 79（同期股價漲 115 倍）。

    合成同構情境用**幾何**序列（價格 ×16、股數 ÷16，兩者相乘恆定）——成交金額全程
    一模一樣、活躍度完全沒變，但股數分位會說「史上最低」。這正是股數分位的失真模式。
    （刻意不用線性序列：線性價 × 線性量是開口向下的拋物線，末端本來就落在低分位，
    那樣測到的是拋物線不是本函式。）"""
    n = 300
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    step = np.arange(n) / (n - 1)
    price = pd.Series(10 * 16 ** step, index=idx)
    vol = pd.Series(1.6e7 * (1 / 16) ** step, index=idx)
    df = pd.DataFrame({"open": price, "high": price * 1.02, "low": price * 0.98,
                       "close": price, "volume": vol}, index=idx)
    st = short_term_traits(df, is_tw=False)
    assert (price * vol).std() / (price * vol).mean() < 1e-9   # 成交額確實恆定
    assert st["vol_pctile"] < 0.05             # 股數：史上最低，看起來像沒人交易
    # 金額：完全沒變 → 分位落在中段。不精算 0.5——浮點讓「恆定」的乘積沒有精確 ties，
    # midrank 因此落在 0.47 附近；斷言區間才是這個測試真正要說的事。
    assert 0.40 < st["turnover_pctile"] < 0.60
    assert st["turnover_pctile"] > st["vol_pctile"]


def test_liquidity_tier_thresholds_by_market():
    """分級門檻是市場慣例（台股億元／美股 5000 萬美元），兩市場不可共用同一組。"""
    tw_thick = short_term_traits(_df(close=100, vol=2_000_000), is_tw=True)   # 2 億
    tw_thin = short_term_traits(_df(close=10, vol=50_000), is_tw=True)        # 50 萬
    assert tw_thick["liquidity_tier"] == "充裕"
    assert tw_thin["liquidity_tier"] == "偏薄"
    assert tw_thick["turnover_unit"] == "TWD"
    # 同一筆 2 億「美元」才算美股充裕；2 億台幣等值的量在美股門檻下是中等
    us = short_term_traits(_df(close=100, vol=200_000), is_tw=False)          # $2000 萬
    assert us["liquidity_tier"] == "中等" and us["turnover_unit"] == "USD"


def test_limit_move_days_is_tw_only():
    """±10% 漲跌停是台股制度，美股沒有 → 該欄必須是 None 而不是 0
    （0 會被讀成「美股沒出現過極端日」，那是假資訊）。"""
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    c = pd.Series([100.0] * (n - 1) + [112.0], index=idx)      # 末根 +12%
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c,
                       "volume": [1e6] * n}, index=idx)
    assert short_term_traits(df, is_tw=True)["limit_move_days_60"] == 1
    assert short_term_traits(df, is_tw=False)["limit_move_days_60"] is None


def test_turnover_rate_needs_shares_outstanding():
    """已發行股數取不到時回 None，不得用任何替代值硬算週轉率。"""
    df = _df(vol=1_000_000)
    assert short_term_traits(df, is_tw=True, shares_out=None)["turnover_rate_pct"] is None
    got = short_term_traits(df, is_tw=True, shares_out=100_000_000)["turnover_rate_pct"]
    assert got == pytest.approx(1.0)


def test_atr_and_intraday_amplitude_are_different_questions():
    """ATR 含隔日跳空、盤中振幅不含。全程無跳空的合成資料兩者應相等；
    加入跳空後只有 ATR 變大——報表把兩者並列就是要讓人看出「波動在哪裡發生」。"""
    flat = _df(spread=0.02)                       # high/low ±2%、無跳空
    st = short_term_traits(flat, is_tw=True)
    assert st["atr14_pct"] == pytest.approx(st["amp_median_pct"], rel=0.01)
    assert st["gap_over_2pct_ratio"] == 0.0

    gapped = flat.copy()
    gapped.iloc[-1, gapped.columns.get_loc("open")] = 110.0    # 末根跳空 +10%
    gapped.iloc[-1, gapped.columns.get_loc("high")] = 112.0
    st2 = short_term_traits(gapped, is_tw=True)
    assert st2["atr14_pct"] > st["atr14_pct"]
    assert st2["gap_over_2pct_ratio"] > 0


# ── 融資變化 ────────────────────────────────────────────────────────────────

def test_margin_chg_absent_column_returns_none():
    """美股 df 沒有 Margin_Balance 欄 → None，不得回 0（0 會被讀成「融資沒變動」）。"""
    assert _margin_chg(_df()) is None


def test_margin_chg_five_day_pct():
    df = _df(n=20)
    df["Margin_Balance"] = [1000.0] * 14 + [1000, 1000, 1000, 1000, 1000, 900.0]
    assert _margin_chg(df) == pytest.approx(-10.0)
