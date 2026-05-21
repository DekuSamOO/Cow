import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest
import pandas as pd
from strategy.walkforward_backtest import calculate_drawdown_series, run_walkforward_backtest

def test_calculate_drawdown_series():
    df = pd.DataFrame({'cum_return': [1.0, 1.1, 0.9, 1.2, 0.8]})
    dd = calculate_drawdown_series(df['cum_return'])
    assert len(dd) == 5
    assert dd.iloc[0] == 0.0
    assert dd.iloc[2] < 0.0
