import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest
from core.season_forecast import get_current_season, forecast_price
from datetime import datetime

def test_get_current_season():
    season, msg, emoji, _, _ = get_current_season(datetime(2025, 1, 1))
    assert isinstance(season, str)

def test_forecast_price():
    target, breakdown = forecast_price(100000)
    assert target > 0
