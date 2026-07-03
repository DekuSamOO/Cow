"""BTC_WATCH._atr_risk_rows re-export 煙霧測試（完整行為覆蓋見 tests/core/test_risk.py）。"""
import numpy as np
import pandas as pd

from BTC_WATCH import _atr_risk_rows


def test_reexport_matches_core_risk():
    idx = pd.date_range("2026-01-01", periods=80, freq="D")
    df = pd.DataFrame({
        "high": np.full(80, 120.0), "low": np.full(80, 80.0),
        "close": np.full(80, 100.0), "ATR": np.full(80, 5.0),
    }, index=idx)
    rows = _atr_risk_rows(df, price=100.0, k=2.0)
    assert len(rows) == 2
    assert "多↓$90" in rows[0] and "空↑$110" in rows[0]
