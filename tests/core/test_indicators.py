import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest
import pandas as pd
from core.indicators import calculate_technical_indicators

def test_calculate_technical_indicators():
    df = pd.DataFrame({
        'open': [10]*50, 'high': [12]*50, 'low': [8]*50, 'close': [11]*50, 'volume': [100]*50
    })
    df.index = pd.date_range('2025-01-01', periods=50)
    df = calculate_technical_indicators(df)
    assert 'SMA_20' in df.columns
    assert 'RSI_14' in df.columns
