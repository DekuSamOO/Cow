"""swing 回測引擎的兩個偏樂觀 bug（2026-11-20 修，余力 B 抓蟲 B4 No.1／No.2）。

1. 多週期回測停損：舊版「本根收盤 < 停損」判定、卻以「本根開盤」成交（偷看收盤）。
   修正：收盤確認 → 次根開盤成交，與 EMA 出場同一口徑。
2. MDD：舊版只用每筆賣出後餘額算，持倉中跌 30% 再反彈出場顯示 0%。
   修正：用逐根／逐日市值曲線。

手算案例一律零摩擦（fee=slippage=0），數字可直接心算核對。
"""
import numpy as np
import pandas as pd
import pytest

import strategy.swing as swing


def _bars(index, opens, closes):
    return pd.DataFrame({"open": opens, "close": closes,
                         "high": np.maximum(opens, closes),
                         "low": np.minimum(opens, closes)}, index=index)


# ── 1. 多週期停損：次根開盤成交 ──────────────────────────────────────────

def test_multitf_stop_fills_at_next_bar_open():
    # 日線兩天、無宏觀過濾：第 1 天 shift 後 False，第 2 天起允許進場
    daily = _bars(pd.DatetimeIndex(["2026-01-01", "2026-01-02"]), [100, 100], [100, 100])
    idx = pd.date_range("2026-01-02 00:00", periods=8, freq="15min")
    # EMA 首根＝收盤，bar0 不成立；bar1 收 102 > EMA → bar2 開盤 100 進場（停損 3% = 97）
    # bar3 開 100、收 96 跌破停損 → 應於 bar4 開盤 90 出場（舊版會在 bar3 開盤 100 出場）
    opens = [100, 100, 100, 100, 90, 90, 90, 90]
    closes = [101, 102, 103, 96, 90, 90, 90, 90]
    m15 = _bars(idx, opens, closes)
    trades, final, roi, n, mdd, _ = swing.run_multitf_backtest(
        daily, m15, "2026-01-01", "2026-01-02", initial_capital=10_000,
        fee_rate=0.0, slippage_rate=0.0, daily_use_sma200=False,
        ema_period_15m=2, rsi_min_15m=0, stop_loss_pct=3.0)
    buy = trades[trades["Type"] == "Buy"].iloc[0]
    sell = trades[trades["Type"] == "Sell"].iloc[0]
    assert buy["Date"] == idx[2] and buy["Price"] == 100
    assert sell["Date"] == idx[4], "停損要在跌破那根（bar3）收盤後的下一根開盤成交"
    assert sell["Price"] == 90, "舊版以 bar3 開盤 100 成交＝偷看收盤，虧損被少算"
    assert sell["Reason"] == "Stop Loss"
    assert sell["PnL%"] == pytest.approx(-10.0)


# ── 2. MDD 含持倉中浮虧 ─────────────────────────────────────────────────

def test_swing_mdd_includes_intratrade_drawdown(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=6, freq="D")
    # 第 0 天訊號 → 第 1 天開盤 100 進場；持倉中收 70（-30%）再反彈；第 4 天訊號 → 第 5 天開盤 105 出場
    df = _bars(idx, [100, 100, 100, 80, 104, 105], [100, 100, 70, 90, 105, 105])
    entry = pd.Series([True, False, False, False, False, False], index=idx)
    exit_ = pd.Series([False, False, False, False, True, False], index=idx)
    monkeypatch.setattr(swing, "compute_entry_mask", lambda *a, **k: entry)
    monkeypatch.setattr(swing, "compute_exit_mask", lambda *a, **k: exit_)
    trades, final, roi, n, mdd, _ = swing.run_swing_strategy_backtest(
        df, idx[0], idx[-1], initial_capital=10_000, fee_rate=0.0, slippage_rate=0.0)
    assert final == pytest.approx(10_500)
    assert mdd == pytest.approx(-30.0), "持倉中 100→70 的回撤不得被漏掉（舊版回 0%）"
