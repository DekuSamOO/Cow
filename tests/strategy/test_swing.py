"""
strategy.swing 工具函數測試

舊版本測試 calculate_swing_signals（P0-2 重構後不存在），改測現存的
calculate_max_drawdown 純函數。實質的進場/出場訊號契約測試已由
tests/test_signal_parity.py 涵蓋。
"""
import numpy as np
import pytest

from strategy.swing import calculate_max_drawdown


def test_max_drawdown_zero_for_monotonic_increase():
    """單調遞增資產曲線：無回撤。"""
    equity = np.array([100.0, 110.0, 120.0, 130.0, 150.0])
    assert calculate_max_drawdown(equity) == 0.0


def test_max_drawdown_simple_dip():
    """100 → 80：50% 回撤後續為 80，最大回撤應為 -20%。"""
    equity = np.array([100.0, 80.0, 90.0])
    result = calculate_max_drawdown(equity)
    assert result == pytest.approx(-20.0, abs=1e-6)


def test_max_drawdown_uses_running_peak():
    """100 → 120 → 60：從峰值 120 跌至 60，回撤應為 -50%（非從 100 算）。"""
    equity = np.array([100.0, 120.0, 60.0])
    result = calculate_max_drawdown(equity)
    assert result == pytest.approx(-50.0, abs=1e-6)


def test_max_drawdown_empty_returns_zero():
    """空 series 不應拋例外。"""
    assert calculate_max_drawdown(np.array([])) == 0.0


def test_max_drawdown_single_value_returns_zero():
    """單一值無法計算回撤。"""
    assert calculate_max_drawdown(np.array([100.0])) == 0.0
