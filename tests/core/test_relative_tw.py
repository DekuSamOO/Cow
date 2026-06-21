"""core/relative_high_tw + relative_low_tw 台股評分純函數測試（固定輸入，零網路）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import pytest

from core.relative_high_tw import compute_relative_high_tw, relative_high_tw_meta
from core.relative_low_tw import compute_relative_low_tw, relative_low_tw_meta


def _df(volume=10_000_000, rsi=50):
    """最小 df：1 列含 volume 欄 + RSI_14（背離偵測在無足夠資料時回 0，安全）。"""
    df = pd.DataFrame({"close": [100.0] * 30, "volume": [volume] * 30})
    row = pd.Series({"RSI_14": rsi, "close": 100.0})
    return row, df


def test_low_empty_chip_all_grey():
    row, df = _df()
    score, sig = compute_relative_low_tw(row, df, chip=None)
    assert score == 0
    assert all(sig[d]["score"] == 0 for d in sig)


def test_low_deep_value_and_chips():
    """PE/PB 低 + 融資暴減 + 法人大買 + 大戶集中 → 高抄底分。"""
    row, df = _df(volume=1_000_000, rsi=18)
    chip = {
        "valuation": {"pe": 8, "pb": 0.9}, "margin": {"fin_chg_pct": -6.0},
        "institutional": {"total_net": 300_000}, "tdcc": {"major_pct": 72, "retail_pct": 10},
    }
    score, sig = compute_relative_low_tw(row, df, chip=chip)
    assert sig["valuation"]["score"] == 25      # PE<10(13) + PB<1(12)
    assert sig["leverage"]["score"] == 20       # 融資 -6%
    assert sig["institution"]["score"] == 20    # 30萬/100萬均量=30% 大買
    assert sig["tdcc"]["score"] == 15           # 大戶 72%
    assert score >= 80
    assert "低估" in relative_low_tw_meta(score)[0]


def test_high_overheat():
    """融資暴增 + 法人大賣 + PE/PB 高 + 散戶鬆散 → 高逃頂分。"""
    row, df = _df(volume=1_000_000, rsi=82)
    chip = {
        "valuation": {"pe": 45, "pb": 6}, "margin": {"fin_chg_pct": 6.0},
        "institutional": {"total_net": -300_000}, "tdcc": {"major_pct": 30, "retail_pct": 45},
    }
    score, sig = compute_relative_high_tw(row, df, chip=chip)
    assert sig["leverage"]["score"] == 20       # 融資 +6%
    assert sig["institution"]["score"] == 25    # -30% 均量 大賣
    assert sig["valuation"]["score"] == 15      # PE≥40(8)+PB≥5(7)
    assert sig["tdcc"]["score"] == 10           # 散戶 45%
    assert score >= 70
    assert "過熱" in relative_high_tw_meta(score)[0] or "逃頂" in relative_high_tw_meta(score)[0]


def test_valuation_absolute_levels():
    row, df = _df()
    # 上櫃缺估值 → 灰燈 0
    assert compute_relative_low_tw(row, df, chip={"valuation": None})[1]["valuation"]["score"] == 0
    # PE 負 → 0
    assert compute_relative_low_tw(row, df, chip={"valuation": {"pe": -5, "pb": 0.8}})[1]["valuation"]["sub"]["pe_score"] == 0


@pytest.mark.parametrize("score,kw", [(70, "低估"), (50, "低估"), (35, "偏冷"), (20, "中性"), (5, "無底部")])
def test_low_meta_levels(score, kw):
    assert kw in relative_low_tw_meta(score)[0]
