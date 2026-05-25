"""
WalkForwardBacktester 工具方法測試

舊版本測試 calculate_drawdown_series / run_walkforward_backtest（P0-2 重構後
不存在）。改測 WalkForwardBacktester class 的 sharpe_ratio 純方法。
進場/出場 mask 一致性由 tests/test_signal_parity.py 鎖定。
"""
import numpy as np
import pandas as pd
import pytest

from strategy.walkforward_backtest import WalkForwardBacktester


@pytest.fixture
def bt():
    return WalkForwardBacktester()


def test_sharpe_ratio_empty_series(bt):
    """空 series 應安全回傳 0.0。"""
    assert bt.sharpe_ratio(pd.Series([], dtype=float)) == 0.0


def test_sharpe_ratio_positive_for_consistent_gains(bt):
    """穩定正報酬序列應給出正 Sharpe。"""
    rng = np.random.default_rng(42)
    returns = pd.Series(rng.normal(loc=0.005, scale=0.01, size=252))
    result = bt.sharpe_ratio(returns)
    assert result > 0, f"穩定正報酬應該得正 Sharpe，實得 {result}"


def test_sharpe_ratio_negative_for_consistent_losses(bt):
    """穩定負報酬序列應給出負 Sharpe。"""
    rng = np.random.default_rng(42)
    returns = pd.Series(rng.normal(loc=-0.005, scale=0.01, size=252))
    result = bt.sharpe_ratio(returns)
    assert result < 0, f"穩定負報酬應該得負 Sharpe，實得 {result}"


def test_backtester_default_config(bt):
    """常數契約：年化日數 365、risk-free 2%。改值請同步檢查 Sharpe 公式。"""
    assert bt.annual_days == 365
    assert bt.risk_free == 0.02
