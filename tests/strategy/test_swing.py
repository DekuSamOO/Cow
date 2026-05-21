import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest
import pandas as pd
from strategy.swing import calculate_swing_signals

def test_calculate_swing_signals():
    df = pd.DataFrame({
        'open': [10]*50, 'high': [12]*50, 'low': [8]*50, 'close': [11]*50, 'volume': [100]*50
    })
    df.index = pd.date_range('2025-01-01', periods=50)
    result = calculate_swing_signals(df)
    assert 'is_bull' in result.columns
