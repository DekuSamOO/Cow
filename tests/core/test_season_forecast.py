import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest
from core.season_forecast import get_current_season, forecast_price
from datetime import datetime


def test_get_current_season():
    # 現行 API 回傳 dict（v1.0 後改版；舊版 5-tuple 已不存在）
    info = get_current_season(datetime(2025, 1, 1))
    assert info is not None
    assert info["season"] in ("spring", "summer", "autumn", "winter")
    assert isinstance(info["season_zh"], str)


def test_forecast_price():
    # 現行 API 回傳 dict，含 target_median / forecast_type
    fc = forecast_price(100000)
    assert fc is not None
    assert fc["target_median"] > 0
    assert fc["forecast_type"] in ("bull_peak", "bear_bottom")
