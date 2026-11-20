"""
tests/test_bear_bottom.py
針對 core/bear_bottom.py 的計分邏輯自動化測試

測試範圍:
  1. calculate_bear_bottom_score() - 單筆即時評分
     - 各指標邊界值測試
     - 總分計算正確性
     - NaN 值處理
  2. score_series() - 向量化批量計算
     - 與單筆計分結果一致性
     - 空 DataFrame 處理
     - 最大/最小分數邊界

執行方式:
  cd /home/user/Cow
  pytest tests/test_bear_bottom.py -v

[Task #10] pytest 測試，覆蓋 bear_bottom.py 核心評分邏輯
"""
import math
import pytest
import numpy as np
import pandas as pd

# 將專案根目錄加入 Python 路徑，確保可以直接 import
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bear_bottom import calculate_bear_bottom_score, score_series


# ────────────────────────────────────────────────────────────────
# 共用 Fixture
# ────────────────────────────────────────────────────────────────

def _make_row(ahr=1.0, mvrv=1.0, pi_gap=5.0, sma200w=2.0,
              puell=1.0, rsi_m=50.0, pl_ratio=5.0, mayer=1.2) -> dict:
    """
    建立單行測試資料 (dict 格式，對應 calculate_bear_bottom_score(row) 的輸入)。
    預設值設定為中性分數（每個指標得 0 或最低分），方便逐項測試。
    """
    return {
        'AHR999':         ahr,
        'MVRV_Z_Proxy':   mvrv,
        'PiCycle_Gap':    pi_gap,
        'SMA200W_Ratio':  sma200w,
        'Puell_Proxy':    puell,
        'RSI_Monthly':    rsi_m,
        'PowerLaw_Ratio': pl_ratio,
        'Mayer_Multiple': mayer,
    }


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """
    建立批量測試 DataFrame (對應 score_series(df) 的輸入)。
    """
    return pd.DataFrame(rows, index=pd.date_range('2021-01-01', periods=len(rows), freq='D'))


# ────────────────────────────────────────────────────────────────
# 測試群組 1: AHR999 指標 (最高 20 分)
# ────────────────────────────────────────────────────────────────

class TestAhr999Scoring:
    """AHR999 囤幣指標評分邏輯測試"""

    def test_ahr999_extreme_bottom(self):
        """< 0.45 → 20 分（歷史抄底區）"""
        row = _make_row(ahr=0.44)
        score, signals = calculate_bear_bottom_score(row)
        assert signals['AHR999']['score'] == 20

    def test_ahr999_undervalued(self):
        """0.45 ~ 0.8 → 13 分（偏低估）"""
        row = _make_row(ahr=0.60)
        score, signals = calculate_bear_bottom_score(row)
        assert signals['AHR999']['score'] == 13

    def test_ahr999_fair_value(self):
        """0.8 ~ 1.2 → 5 分（合理區間）"""
        row = _make_row(ahr=1.0)
        score, signals = calculate_bear_bottom_score(row)
        assert signals['AHR999']['score'] == 5

    def test_ahr999_overvalued(self):
        """>= 1.2 → 0 分（高估）"""
        row = _make_row(ahr=1.5)
        score, signals = calculate_bear_bottom_score(row)
        assert signals['AHR999']['score'] == 0

    def test_ahr999_boundary_exact(self):
        """邊界值精確測試：0.45 本身應得 13 分（0.45 <= v < 0.8）"""
        row = _make_row(ahr=0.45)
        score, signals = calculate_bear_bottom_score(row)
        assert signals['AHR999']['score'] == 13

    def test_ahr999_boundary_upper(self):
        """邊界值精確測試：0.8 本身應得 5 分"""
        row = _make_row(ahr=0.80)
        score, signals = calculate_bear_bottom_score(row)
        assert signals['AHR999']['score'] == 5


# ────────────────────────────────────────────────────────────────
# 測試群組 2: MVRV Z-Score (最高 18 分)
# ────────────────────────────────────────────────────────────────

class TestMvrvScoring:
    """MVRV Z-Score Proxy 評分測試"""

    def test_mvrv_strong_bottom(self):
        """< -1.0 → 18 分（強力底部）"""
        row = _make_row(mvrv=-1.5)
        _, signals = calculate_bear_bottom_score(row)
        assert signals['MVRV_Z_Proxy']['score'] == 18

    def test_mvrv_undervalued(self):
        """-1.0 ~ 0 → 12 分（低估）"""
        row = _make_row(mvrv=-0.5)
        _, signals = calculate_bear_bottom_score(row)
        assert signals['MVRV_Z_Proxy']['score'] == 12

    def test_mvrv_neutral(self):
        """0 ~ 2.0 → 4 分（中性）"""
        row = _make_row(mvrv=1.0)
        _, signals = calculate_bear_bottom_score(row)
        assert signals['MVRV_Z_Proxy']['score'] == 4

    def test_mvrv_overheated(self):
        """>= 2.0 → 0 分（高估/頂部）"""
        row = _make_row(mvrv=3.5)
        _, signals = calculate_bear_bottom_score(row)
        assert signals['MVRV_Z_Proxy']['score'] == 0


# ────────────────────────────────────────────────────────────────
# 測試群組 3: 複合總分計算
# ────────────────────────────────────────────────────────────────

class TestTotalScoreCalculation:
    """複合總分計算正確性測試"""

    def test_all_maximum_score(self):
        """
        所有指標都在最高分區間時，總分應等於各指標最高分之和:
        AHR999(20) + MVRV(18) + PiCycle(15) + SMA200W(15) +
        Puell(12) + RSI_M(10) + PowerLaw(5) + Mayer(5) = 100
        """
        row = _make_row(
            ahr=0.3,      # < 0.45 → 20
            mvrv=-1.5,    # < -1.0 → 18
            pi_gap=-15.0, # < -10  → 15
            sma200w=0.8,  # < 1.0  → 15
            puell=0.3,    # < 0.5  → 12
            rsi_m=25.0,   # < 30   → 10
            pl_ratio=1.5, # < 2.0  → 5
            mayer=0.7,    # < 0.8  → 5
        )
        score, signals = calculate_bear_bottom_score(row)
        assert score == 100, f"最高分應為 100，實際得 {score}"

    def test_all_zero_score(self):
        """
        所有指標都在 0 分區間時，總分應為 0
        """
        row = _make_row(
            ahr=2.0,       # >= 1.2 → 0
            mvrv=5.0,      # >= 2.0 → 0
            pi_gap=20.0,   # >= 5   → 0
            sma200w=5.0,   # >= 4.0 → 0
            puell=2.0,     # >= 1.5 → 0
            rsi_m=80.0,    # >= 55  → 0
            pl_ratio=15.0, # >= 10  → 0
            mayer=2.0,     # >= 1.5 → 0
        )
        score, signals = calculate_bear_bottom_score(row)
        assert score == 0, f"最低分應為 0，實際得 {score}"

    def test_score_is_sum_of_signals(self):
        """總分應等於各 signal 分數之和"""
        row = _make_row(ahr=0.6, mvrv=0.5, pi_gap=0.0, sma200w=1.5,
                        puell=0.6, rsi_m=35.0, pl_ratio=3.0, mayer=0.9)
        score, signals = calculate_bear_bottom_score(row)
        expected = sum(v['score'] for v in signals.values())
        assert score == expected, f"總分 {score} 不等於 signals 之和 {expected}"

    def test_score_range(self):
        """分數必須在 [0, 100] 範圍內"""
        row = _make_row(ahr=0.44, mvrv=-1.5, pi_gap=-15.0, sma200w=0.8,
                        puell=0.3, rsi_m=25.0, pl_ratio=1.5, mayer=0.7)
        score, _ = calculate_bear_bottom_score(row)
        assert 0 <= score <= 100


# ────────────────────────────────────────────────────────────────
# 測試群組 4: NaN 值處理
# ────────────────────────────────────────────────────────────────

class TestNanHandling:
    """NaN/None 值不應導致程式崩潰"""

    def test_none_values_dont_crash(self):
        """None 值應被靜默忽略（該指標得 0 分但不拋出例外）"""
        row = {'AHR999': None, 'MVRV_Z_Proxy': None}
        try:
            score, signals = calculate_bear_bottom_score(row)
        except Exception as e:
            pytest.fail(f"None 值導致例外: {e}")

    def test_nan_values_dont_crash(self):
        """math.nan 值應被靜默忽略"""
        row = _make_row(ahr=float('nan'), mvrv=float('nan'))
        try:
            score, signals = calculate_bear_bottom_score(row)
        except Exception as e:
            pytest.fail(f"NaN 值導致例外: {e}")

    def test_missing_keys_dont_crash(self):
        """缺少欄位的 dict 不應崩潰（使用 row.get() 的 None fallback）"""
        row = {}  # 完全空的 row
        try:
            score, signals = calculate_bear_bottom_score(row)
            assert score == 0
        except Exception as e:
            pytest.fail(f"空 row 導致例外: {e}")


# ────────────────────────────────────────────────────────────────
# 測試群組 5: score_series() 向量化批量計算
# ────────────────────────────────────────────────────────────────

class TestScoreSeries:
    """score_series() 向量化批量計算一致性測試"""

    def test_score_series_consistency_with_single(self):
        """
        score_series() 的批量結果應與 calculate_bear_bottom_score() 的逐行結果一致。
        這是向量化重構後最重要的正確性驗證。
        """
        rows = [
            _make_row(ahr=0.3, mvrv=-1.5, pi_gap=-15.0, sma200w=0.8,
                      puell=0.3, rsi_m=25.0, pl_ratio=1.5, mayer=0.7),   # 滿分
            _make_row(ahr=1.5, mvrv=3.0, pi_gap=15.0, sma200w=5.0,
                      puell=2.0, rsi_m=70.0, pl_ratio=12.0, mayer=2.0),  # 零分
            _make_row(ahr=0.6, mvrv=-0.5, pi_gap=-5.0, sma200w=1.2,
                      puell=0.7, rsi_m=35.0, pl_ratio=3.0, mayer=0.9),   # 中間分
        ]
        df = _make_df(rows)

        # 向量化批量計算
        batch_scores = score_series(df)

        # 逐行計算對照
        for i, row in enumerate(rows):
            single_score, _ = calculate_bear_bottom_score(row)
            batch_score = int(batch_scores.iloc[i])
            assert batch_score == single_score, (
                f"第 {i} 行: score_series()={batch_score} vs "
                f"calculate_bear_bottom_score()={single_score}"
            )

    def test_score_series_empty_df(self):
        """空 DataFrame 應返回空 Series，不崩潰"""
        df = pd.DataFrame()
        result = score_series(df)
        assert len(result) == 0

    def test_score_series_max_score(self):
        """最高分 row 應得 100"""
        rows = [_make_row(ahr=0.3, mvrv=-1.5, pi_gap=-15.0, sma200w=0.8,
                          puell=0.3, rsi_m=25.0, pl_ratio=1.5, mayer=0.7)]
        df = _make_df(rows)
        scores = score_series(df)
        assert int(scores.iloc[0]) == 100

    def test_score_series_min_score(self):
        """最低分 row 應得 0"""
        rows = [_make_row(ahr=2.0, mvrv=5.0, pi_gap=20.0, sma200w=5.0,
                          puell=2.0, rsi_m=80.0, pl_ratio=15.0, mayer=2.0)]
        df = _make_df(rows)
        scores = score_series(df)
        assert int(scores.iloc[0]) == 0

    def test_score_series_returns_integers(self):
        """score_series() 應返回整數 Series（方便前端顯示）"""
        rows = [_make_row(), _make_row(ahr=0.3)]
        df = _make_df(rows)
        scores = score_series(df)
        assert scores.dtype in (int, 'int64', 'int32'), f"dtype 應為整數，實際為 {scores.dtype}"

    def test_score_series_length_matches_input(self):
        """返回長度應與輸入 DataFrame 行數一致"""
        rows = [_make_row() for _ in range(10)]
        df = _make_df(rows)
        scores = score_series(df)
        assert len(scores) == 10


# ────────────────────────────────────────────────────────────────
# 月線 RSI point-in-time（2026-11-20，余力 B 抓蟲 B2 No.1）
# 舊版 resample('MS').last() 讓整個月吃到月底收盤＝歷史 look-ahead。
# ────────────────────────────────────────────────────────────────

import pandas_ta as _ta
from core.bear_bottom import _monthly_rsi_pit


def _walk(days=1000, seed=7):
    """年週期正弦＋小雜訊：讓月線 RSI 落在 30～70 之間（審查建議，避免全程極端值）。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-15", periods=days, freq="D")
    t = np.arange(days)
    log_p = 0.6 * np.sin(2 * np.pi * t / 365) + np.cumsum(rng.normal(0, 0.01, days))
    return pd.Series(10_000 * np.exp(log_p), index=idx)


def test_monthly_rsi_walk_spans_scoring_range():
    """前提檢查：測試序列的 RSI 要跨過計分門檻區，否則下面的性質測試只在極端值上成立。"""
    r = _monthly_rsi_pit(_walk()).dropna()
    assert r.min() < 50 < r.max() and r.max() - r.min() > 15


def test_monthly_rsi_month_end_equals_completed_month_rsi():
    """每月最後一天＝該月已收完，值必須等於標準月線 RSI（正向錨點）。"""
    close = _walk()
    new = _monthly_rsi_pit(close)
    mc = close.resample("ME").last()
    std = _ta.rsi(mc, length=14)
    n = 0
    for me, v in std.dropna().items():
        # 開頭不足 14 個完整月：ta.rsi 在全序列上仍會吐值（ewm 回推），新版刻意給 NaN，不比
        if me in new.index and not math.isnan(new.loc[me]):
            assert new.loc[me] == pytest.approx(v)
            n += 1
    assert n >= 10


def test_monthly_rsi_nan_close_gives_nan():
    close = _walk()
    close.loc["2022-06-10"] = np.nan
    r = _monthly_rsi_pit(close)
    assert math.isnan(r.loc["2022-06-10"])
    assert not math.isnan(r.loc["2022-06-09"])


def test_monthly_rsi_is_point_in_time():
    """截到任一日重算，該日的值必須與全資料算出的相同（不能看到之後的收盤）。"""
    close = _walk()
    full = _monthly_rsi_pit(close)
    for t in ["2021-06-03", "2021-11-30", "2022-04-15", "2022-08-01"]:
        trunc = _monthly_rsi_pit(close.loc[:t])
        assert not math.isnan(full.loc[t])
        assert trunc.loc[t] == pytest.approx(full.loc[t]), f"{t} 偷看到之後的收盤"


def test_monthly_rsi_unaffected_by_later_close_in_same_month():
    """把月底收盤改掉，同月較早日子的值不得改變（舊版會變＝look-ahead 的直接寫照）。"""
    close = _walk()
    a = _monthly_rsi_pit(close)
    bumped = close.copy()
    bumped.loc["2022-03-31"] *= 0.5
    b = _monthly_rsi_pit(bumped)
    early = slice("2022-03-01", "2022-03-30")
    assert np.allclose(a.loc[early], b.loc[early])
    assert b.loc["2022-03-31"] != pytest.approx(a.loc["2022-03-31"])


def test_monthly_rsi_last_row_matches_previous_live_value():
    """最後一列＝即時推播用值，須與舊算法一致（不可退化成上個月的 RSI）。"""
    close = _walk()
    old = _ta.rsi(close.resample("MS").last(), length=14).reindex(close.index).ffill()
    assert _monthly_rsi_pit(close).iloc[-1] == pytest.approx(old.iloc[-1])


def test_monthly_rsi_nan_until_enough_completed_months():
    close = _walk(days=200)                 # 不到 14 個完整月
    assert _monthly_rsi_pit(close).isna().all()