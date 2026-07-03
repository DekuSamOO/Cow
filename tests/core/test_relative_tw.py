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
    """v0.4 校準：融資暴減(提權40) + 技術回穩(提權30) + 法人大買(20) + 估值低(降權10) → 高抄底分。
    tdcc 大戶維已移除（AUC 0.422 方向反）；leverage/technical 用 rescale_dim 吸收其 15 分。"""
    row, df = _df(volume=1_000_000, rsi=18)
    chip = {
        "valuation": {"pe": 8, "pb": 0.9}, "margin": {"fin_chg_pct": -6.0},
        "institutional": {"total_net": 300_000}, "tdcc": {"major_pct": 72, "retail_pct": 10},
    }
    score, sig = compute_relative_low_tw(row, df, chip=chip)
    assert "tdcc" not in sig                     # 大戶維已移除（方向反）
    assert sig["leverage"]["score"] == 40        # 融資 ≤-5% 滿分，rescale 30→40（最強維）
    assert sig["institution"]["score"] == 20     # 30萬/100萬均量=30% 大買（不變）
    assert sig["valuation"]["score"] == 10       # PE<10(5)+PB<1(5)（已降權，不變）
    assert sig["technical"]["score"] == 10       # RSI≤20(8) rescale 25→30 → 10
    assert score >= 80
    assert "低估" in relative_low_tw_meta(score)[0]


def _df_vol_spike(n=60, base=1_000_000, last=10_000_000, rsi=82):
    """60 根 df，最後一根爆量（量能分位≈99）→ 觸發 v0.3 量能見頂維。"""
    vols = [base] * (n - 1) + [last]
    df = pd.DataFrame({"close": [100.0] * n, "volume": vols})
    row = pd.Series({"RSI_14": rsi, "close": 100.0})
    return row, df


def test_high_overheat():
    """v0.3 校準：估值高(最強30) + 量能見頂(新18) + 融資暴增(10) + 法人大賣(降權4) + 散戶鬆散(8) → 高逃頂分。"""
    row, df = _df_vol_spike(rsi=82)
    chip = {
        "valuation": {"pe": 45, "pb": 6}, "margin": {"fin_chg_pct": 6.0},
        "institutional": {"total_net": -500_000}, "tdcc": {"major_pct": 30, "retail_pct": 45},
    }
    score, sig = compute_relative_high_tw(row, df, chip=chip)
    assert sig["valuation"]["score"] == 30      # PE≥40(16)+PB≥5(14)（最強維）
    assert sig["volume"]["score"] == 18         # 爆量 ≥95 分位（v0.3 新維）
    assert sig["leverage"]["score"] == 10       # 融資 ≥5%（max 15→10）
    assert sig["institution"]["score"] == 4     # ≤-20% 均量 大賣（max 10→4）
    assert sig["tdcc"]["score"] == 8            # 散戶 45%（max 15→8）
    assert sig["technical"]["score"] == 10      # RSI≥80(10)，無背離
    assert score >= 70
    assert "過熱" in relative_high_tw_meta(score)[0] or "逃頂" in relative_high_tw_meta(score)[0]


def test_high_volume_pctile_dim():
    """量能見頂維：定值量→0.5 分位→0 分；末根爆量→高分位→滿分；歷史不足→灰燈 0。"""
    # 定值 volume（非爆量）→ midrank 0.5 → 0 分
    row, df = _df(volume=5_000_000, rsi=50)
    df = pd.concat([df] * 2, ignore_index=True)   # 60 根定值
    assert compute_relative_high_tw(row, df, chip=None)[1]["volume"]["score"] == 0
    # 末根爆量 → 滿分 18
    row2, df2 = _df_vol_spike(rsi=50)
    assert compute_relative_high_tw(row2, df2, chip=None)[1]["volume"]["score"] == 18
    # 歷史 <60 根 → 資料不足灰燈 0
    row3, df3 = _df(volume=1_000_000, rsi=50)   # 30 根
    assert compute_relative_high_tw(row3, df3, chip=None)[1]["volume"]["score"] == 0


def test_weights_sum():
    """v0.5/v0.4（2026-07-02 回測拍板）：
    逃頂 核心六維 100 + vol_price 8（已驗證疊加，clamp 100）= 理論 108；抄底四維恰 100。
    兩側皆無「未擬合」維（vol_price 逃頂轉正式、其餘雜訊/反指標維已移除）。"""
    from core.relative_high_tw import WEIGHTS_HIGH_TW, UNFITTED_DIMS_HIGH_TW
    from core.relative_low_tw import WEIGHTS_LOW_TW, UNFITTED_DIMS_LOW_TW
    assert UNFITTED_DIMS_HIGH_TW == () and UNFITTED_DIMS_LOW_TW == ()
    # 逃頂核心（排除疊加的 vol_price）恰 100；含疊加 108
    high_core = {k: v for k, v in WEIGHTS_HIGH_TW.items() if k != "vol_price"}
    assert sum(high_core.values()) == 100
    assert sum(WEIGHTS_HIGH_TW.values()) == 108
    # 抄底四維恰 100（無疊加、無反指標維）
    assert sum(WEIGHTS_LOW_TW.values()) == 100
    assert set(WEIGHTS_LOW_TW) == {"leverage", "technical", "institution", "valuation"}


def test_score_clamped_at_100_with_overlay_dims():
    """逃頂 vol_price 疊加（max 8）滿分時，總分仍 clamp 在 100 不會溢出；structure 已移除。"""
    row, df = _df_vol_spike(rsi=82)
    chip = {
        "valuation": {"pe": 45, "pb": 6}, "margin": {"fin_chg_pct": 6.0},
        "institutional": {"total_net": -500_000}, "tdcc": {"major_pct": 30, "retail_pct": 45},
    }
    score, sig = compute_relative_high_tw(row, df, chip=chip)
    assert score <= 100
    assert sig["vol_price"]["max"] == 8
    assert "structure" not in sig


def test_valuation_absolute_levels():
    row, df = _df()
    # 上櫃缺估值 → 灰燈 0
    assert compute_relative_low_tw(row, df, chip={"valuation": None})[1]["valuation"]["score"] == 0
    # PE 負 → 0
    assert compute_relative_low_tw(row, df, chip={"valuation": {"pe": -5, "pb": 0.8}})[1]["valuation"]["sub"]["pe_score"] == 0


@pytest.mark.parametrize("score,kw", [(70, "低估"), (50, "低估"), (35, "偏冷"), (20, "中性"), (5, "無底部")])
def test_low_meta_levels(score, kw):
    assert kw in relative_low_tw_meta(score)[0]
