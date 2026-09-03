import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest
import pandas as pd
from core.season_forecast import get_current_season, forecast_price
from datetime import datetime


def _mk_df(n=400, start=None):
    """暖機夠長的日線 df（sma200 需 200 根、市場軸回溯窗 15 根）。

    刻意造一段「持續上行後回落」的路徑，讓 dd/sma200 兩個輸入都拿得到值，
    走的是有市場資訊的真實象限，而不是無資料退化格。
    """
    idx = pd.date_range(start or "2025-06-01", periods=n)
    up = list(range(50000, 50000 + n * 200, 200))[:n]
    close = up[: n - 40] + [up[n - 41] - i * 300 for i in range(1, 41)]
    return pd.DataFrame({"close": close[:n], "high": close[:n],
                         "low": close[:n], "open": close[:n]}, index=idx)


def test_get_current_season():
    # 現行 API 回傳 dict（v1.0 後改版；舊版 5-tuple 已不存在）
    info = get_current_season(datetime(2025, 1, 1))
    assert info is not None
    assert info["season"] in ("spring", "summer", "autumn", "winter")
    assert isinstance(info["season_zh"], str)


def test_forecast_price():
    """現行 API 回傳 dict，含 target_median / forecast_type。

    ⚠️ 2026-09-03 改寫：原本寫死 `target_median > 0`，在 SEASON_ENGINE 切成 "v2"
    之後會 TypeError（None > int）。**不帶 df 時 v2 的市場軸拿不到 dd/sma200，
    退化成 `mid`，落到 autumn×mid → `observe`（刻意不出目標價）。**

    這不是回歸——**全部 5 個生產呼叫端都有帶 df**（2026-09-03 grep 實查：
    `relative_high.py:419`（另有 `df is not None` 守門）／`:468`、
    `daily_line_notify.py:164`、`tab_macro_compass.py:1073`、`test_flex_message.py:98`），
    無 df 路徑只存在於測試。故本測試改為**帶 df 驗真實路徑**，
    另用一條獨立測試把「無 df → observe」這個退化行為釘住，不讓它默默改變。
    """
    df = _mk_df()
    fc = forecast_price(100000, df=df)
    assert fc is not None
    assert fc["forecast_type"] in ("bull_peak", "bear_bottom", "observe")
    if fc["forecast_type"] == "observe":
        assert fc["target_median"] is None          # observe 就是不出目標價
    else:
        assert fc["target_median"] > 0


def test_forecast_price_without_df_degrades_to_observe():
    """不帶 df → 市場軸拿不到 dd/sma200 → 退化成 mid。

    生產端不會走到這條路（5 個呼叫端都帶 df），但這個退化行為必須是**明示的**：
    哪天有人新增一個不帶 df 的呼叫端，應該要被這條測試提醒「你會拿到 observe」，
    而不是拿到一個看起來正常、其實沒有市場資訊的目標價。
    """
    fc = forecast_price(100000)
    assert fc is not None
    assert fc["season_engine"] == "v2"
    assert fc["forecast_type"] == "observe"
    assert fc["target_median"] is None
