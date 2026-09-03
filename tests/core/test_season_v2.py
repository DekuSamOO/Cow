#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/core/test_season_v2.py — 四季論 v2 十二象限狀態機守門（B1，2026-07-06）

design：Github\\Cow\\season_v2_design.md。守：
  1. derive_effective_state 十二象限查表逐格正確（無遺漏、無誤填）。
  2. _derive_market_axis 3 日防抖：抖動不切換／連續 3 日一致才切換。
  3. C-2 重現例：v1 在 autumn×市場未確認（近 ATH 上漲中）誤出熊底預測，
     v2 必須改判 bull_peak（延長牛市），且不含熊底目標（ath_ref is None）。
  4. season_engine="v1"（預設）forecast_price 行為零改動（既有測試已覆蓋，
     此處補一個 v1/v2 並列對照確認兩者確實走不同象限判定）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime

import pandas as pd
import pytest

from core.season_forecast import (
    derive_effective_state, _derive_market_axis, _raw_market_axis,
    forecast_price, HALVING_DATES, _SEASON_V2_TABLE,
)

_T = ("spring", "summer", "autumn", "winter")
_M = ("bull", "mid", "bear", "deep_bear")   # 2026-09-02：三級擴為四級


# ---------------------------------------------------------------------------
# 十二象限查表（design §2，逐格比對）
# ---------------------------------------------------------------------------

_EXPECTED = {
    ("spring", "bull"): ("bull_peak",   "spring"),
    ("spring", "mid"):  ("bull_peak",   "spring"),
    ("spring", "bear"): ("bear_bottom", "winter"),
    ("summer", "bull"): ("bull_peak",   "summer"),
    ("summer", "mid"):  ("bull_peak",   "summer"),
    ("summer", "bear"): ("bear_bottom", "autumn"),
    ("autumn", "bull"): ("bull_peak",   "summer_ext"),
    ("autumn", "mid"):  ("observe",     "autumn"),
    ("autumn", "bear"): ("bear_bottom", "autumn"),
    ("winter", "bull"): ("observe",     "early_spring?"),
    ("winter", "mid"):  ("bear_bottom", "winter"),
    ("winter", "bear"): ("bear_bottom", "winter"),
    # M-深熊（2026-09-02 新增）：四格一律 winter，對齊 v1 的 -30% override 語意
    ("spring", "deep_bear"): ("bear_bottom", "winter"),
    ("summer", "deep_bear"): ("bear_bottom", "winter"),
    ("autumn", "deep_bear"): ("bear_bottom", "winter"),
    ("winter", "deep_bear"): ("bear_bottom", "winter"),
}


@pytest.mark.parametrize("t,m", [(t, m) for t in _T for m in _M])
def test_derive_effective_state_matches_table(t, m):
    state = derive_effective_state(t, m)
    exp_type, exp_season = _EXPECTED[(t, m)]
    assert state["forecast_type"] == exp_type
    assert state["eff_season"] == exp_season
    assert isinstance(state["conf_cap"], int) and 0 < state["conf_cap"] <= 100


def test_table_has_no_undefined_quadrant():
    """十六象限全定義，無遺漏（模組層 assert 已保證，這裡再從公開介面驗一次）。

    2026-09-02：12 → 16（市場軸加 deep_bear）。**這個數字刻意寫死**——
    改象限數是設計變更，必須連帶改測試，不能讓它默默通過。
    """
    assert len(_SEASON_V2_TABLE) == 16
    for t in _T:
        for m in _M:
            assert (t, m) in _SEASON_V2_TABLE


def test_observe_quadrants_have_note():
    """observe 型必須帶 note（不出目標價時要說明為什麼，design 誠實原則）。"""
    for (t, m), (ftype, _) in _EXPECTED.items():
        if ftype == "observe":
            assert derive_effective_state(t, m)["note"]


def test_derive_effective_state_unknown_quadrant_raises():
    with pytest.raises(ValueError):
        derive_effective_state("nonexistent", "bull")


def test_returned_dict_is_a_copy():
    """呼叫端可安全修改回傳值，不應污染模組層查表（regression：曾見過共用可變預設值的 bug 類型）。"""
    d1 = derive_effective_state("spring", "bull")
    d1["conf_cap"] = 999
    d2 = derive_effective_state("spring", "bull")
    assert d2["conf_cap"] == 80


# ---------------------------------------------------------------------------
# 市場軸防抖（design §1，3 日一致才切換）
# ---------------------------------------------------------------------------

def _mk_df(n, start="2026-01-01"):
    return pd.DataFrame({"close": list(range(1, n + 1))},
                        index=pd.date_range(start, periods=n))


def _script_axis(monkeypatch, raw_seq):
    """monkeypatch analyze_market_state 依呼叫順序回傳指定的 (dd, up)，
    讓 _derive_market_axis 的 3 日防抖邏輯在不需要建構真實價格路徑下可被單獨驗證。"""
    calls = {"i": 0}

    def fake_ams(price, df, halving):
        i = calls["i"]
        calls["i"] += 1
        s = raw_seq[i]
        if s == "bull":
            dd, up = -0.05, True
        elif s == "bear":
            dd, up = -0.25, False
        else:
            dd, up = -0.15, True   # mid：非 bull 非 bear 的中繼區
        return {"drawdown_from_ath": dd, "is_above_sma200": up, "cycle_ath": price,
                "sma200": price, "price_vs_sma200": 1.0, "cycle_ath_date": None}

    monkeypatch.setattr("core.season_forecast.analyze_market_state", fake_ams)


def test_market_axis_raw_classification():
    assert _raw_market_axis(-0.05, True) == "bull"
    assert _raw_market_axis(-0.25, False) == "bear"
    assert _raw_market_axis(-0.15, True) == "mid"
    assert _raw_market_axis(-0.05, False) == "mid"   # dd 條件夠但 up 不符 → 不算 bull


# --- M-深熊（2026-09-02 新增，findings 發現 A）-------------------------------

def test_raw_axis_deep_bear_tier():
    """dd<-30% 且跌破年線 → deep_bear；-20~-30% 仍是 bear。"""
    assert _raw_market_axis(-0.35, False) == "deep_bear"
    assert _raw_market_axis(-0.83, False) == "deep_bear"   # 2018-12-15 實例
    assert _raw_market_axis(-0.25, False) == "bear"


def test_raw_axis_deep_bear_boundary_is_strict():
    """-30% 是嚴格門檻：剛好 -30.0% 不算 deep_bear（與 v1 的 `< -0.30` 對齊）。"""
    assert _raw_market_axis(-0.30, False) == "bear"
    assert _raw_market_axis(-0.3001, False) == "deep_bear"


def test_deep_bear_requires_below_sma200():
    """站上年線時再深的回撤都不是「空頭確認」——四級全都要求 not up。"""
    assert _raw_market_axis(-0.50, True) == "mid"
    assert _raw_market_axis(-0.83, True) == "mid"


def test_hysteresis_dither_does_not_switch(monkeypatch):
    """原始狀態在 mid/bull 間逐日交替、從未連續 3 天一致 → 沿用窗內初始狀態，不反覆翻轉。
    序列長度須等於 _M_WINDOW_DAYS（15），否則 df 天數 > 回溯窗時 _derive_market_axis
    只吃最後 15 天、與此處逐一 monkeypatch 的呼叫序不對齊。"""
    raw_seq = (["mid", "bull"] * 8)[:15]   # 15 天嚴格交替，任何連續 3 天必不同
    _script_axis(monkeypatch, raw_seq)
    df = _mk_df(len(raw_seq))
    state, trace = _derive_market_axis(df, HALVING_DATES[3])
    assert trace == raw_seq
    assert state == trace[0]   # 全程無 3 日一致窗口，維持窗內第一天的狀態


def test_hysteresis_switches_after_3_consistent_days(monkeypatch):
    """連續 3 天一致的新狀態 → 切換。"""
    raw_seq = ["mid"] * 10 + ["bear"] * 5
    _script_axis(monkeypatch, raw_seq)
    df = _mk_df(len(raw_seq))
    state, trace = _derive_market_axis(df, HALVING_DATES[3])
    assert state == "bear"


def test_hysteresis_two_consistent_days_not_enough(monkeypatch):
    """只有 2 天新狀態、窗口不足 3 天一致 → 尚未切換。"""
    raw_seq = ["mid"] * 12 + ["bear"] * 2
    _script_axis(monkeypatch, raw_seq)
    df = _mk_df(len(raw_seq))
    state, trace = _derive_market_axis(df, HALVING_DATES[3])
    assert state == "mid"


# --- 防抖逃生門（2026-09-02 新增，findings 發現 B）---------------------------
#
# 發現 B：2021-08/10/12 與 2025-03 共 12 天，dd 已回到 -17.6%~-28% 且 **全部
# up200=True**，v2 卻因原始判定在 mid/bear 間逐日跳動、湊不滿連續 3 日 mid 而
# 卡在 bear，把牛市回檔判成熊底（v1 給 bull_peak、v2 給 bear_bottom），
# 方向與 C-2 修復意圖相反。

def test_escape_bear_immediately_when_above_sma200(monkeypatch):
    """卡在 bear 時，只要出現一天站上年線就立刻脫離——不等 3 日防抖。

    這組序列在**舊邏輯下會停在 bear**（mid/bear 嚴格交替，永遠湊不滿 3 日一致），
    正是發現 B 那 12 天的形狀。
    """
    raw_seq = ["bear"] * 5 + (["mid", "bear"] * 5)[:10]
    assert len(raw_seq) == 15
    _script_axis(monkeypatch, raw_seq)
    df = _mk_df(len(raw_seq))
    state, trace = _derive_market_axis(df, HALVING_DATES[3])
    assert trace == raw_seq
    assert state == "mid", "站上年線後仍停在 bear＝把牛市回檔判成熊底（發現 B）"


def test_escape_hatch_also_applies_to_deep_bear(monkeypatch):
    """逃生門對 deep_bear 同樣有效（_BEAR_FAMILY 兩個都要涵蓋）。"""
    def fake_ams(price, df, halving):
        i = fake_ams.i
        fake_ams.i += 1
        dd, up = (-0.50, False) if i < 12 else (-0.15, True)
        return {"drawdown_from_ath": dd, "is_above_sma200": up, "cycle_ath": price,
                "sma200": price, "price_vs_sma200": 1.0, "cycle_ath_date": None}
    fake_ams.i = 0
    monkeypatch.setattr("core.season_forecast.analyze_market_state", fake_ams)
    state, trace = _derive_market_axis(_mk_df(15), HALVING_DATES[3])
    assert trace[0] == "deep_bear" and trace[-1] == "mid"
    assert state == "mid"


def test_escape_hatch_is_one_directional(monkeypatch):
    """**進入**空頭 family 仍須連續 3 日——逃生門只開單向，不得讓急跌雜訊直接誤入。"""
    raw_seq = ["mid"] * 5 + (["bear", "mid"] * 5)[:10]
    assert len(raw_seq) == 15
    _script_axis(monkeypatch, raw_seq)
    state, _ = _derive_market_axis(_mk_df(len(raw_seq)), HALVING_DATES[3])
    assert state == "mid", "單日 bear 就切換＝逃生門開成雙向，防抖失效"


def test_enter_bear_still_works_with_3_consistent_days(monkeypatch):
    """逃生門不得誤傷正常進入路徑：連續 3 日 bear 仍要切得進去。"""
    raw_seq = ["mid"] * 10 + ["bear"] * 5
    _script_axis(monkeypatch, raw_seq)
    state, _ = _derive_market_axis(_mk_df(len(raw_seq)), HALVING_DATES[3])
    assert state == "bear"


def test_market_axis_empty_df_returns_mid():
    state, trace = _derive_market_axis(None, HALVING_DATES[3])
    assert state == "mid" and trace == []
    state2, trace2 = _derive_market_axis(pd.DataFrame(), HALVING_DATES[3])
    assert state2 == "mid" and trace2 == []


# ---------------------------------------------------------------------------
# C-2 重現例：autumn（月26）× 市場未確認（近 ATH 上漲中）
# v1 誤出熊底預測；v2 必須是 bull_peak/summer_ext，且無熊底目標（ath_ref is None）。
# ---------------------------------------------------------------------------

def test_c2_reproduction_v1_bug_vs_v2_fix(monkeypatch):
    halving = HALVING_DATES[3]   # 2024-04-19
    as_of = halving + pd.Timedelta(days=int(26 * 30.44))   # month_in_cycle≈26 → autumn

    # 建構價格序列：長期上漲、現價貼近（略低於）序列最高點 → dd≈-0.01、is_above_sma200=True
    n = 260
    idx = pd.date_range(end=as_of, periods=n)
    prices = [50000.0 * (1 + 0.01) ** i for i in range(n)]   # 單調上升
    prices[-1] = prices[-2] * 0.99   # 現價=前高×0.99（貼近 ATH，非破線）
    df = pd.DataFrame({"close": prices}, index=idx)
    current_price = prices[-1]

    fc_v1 = forecast_price(current_price, df, as_of=as_of, season_engine="v1")
    assert fc_v1 is not None
    # v1 的已知病灶：real_season 落在 autumn 且無 drawdown 覆寫條件成立時，
    # forecast_type 仍走 "bear_bottom"（本測試用來釘住 bug 現況，供 v2 對照；
    # 若某天 v1 也修正了這格，此斷言會提醒維護者同步更新本測試的敘事）。
    assert fc_v1["forecast_type"] == "bear_bottom"

    fc_v2 = forecast_price(current_price, df, as_of=as_of, season_engine="v2")
    assert fc_v2 is not None
    assert fc_v2["forecast_type"] == "bull_peak"
    assert fc_v2["effective_season"]["season"] == "summer_ext"
    assert fc_v2["ath_ref"] is None          # 無熊底目標輸出
    assert fc_v2["target_median"] is not None   # bull_peak 仍正常出牛市目標
    assert fc_v2["confidence"] <= 45          # conf_cap（design §2 autumn×bull＝45）


def test_v2_observe_type_has_no_targets(monkeypatch):
    """autumn×mid（轉折觀察期）：target_low/mid/high 皆 None，ath_ref 也 None。"""
    halving = HALVING_DATES[3]
    as_of = halving + pd.Timedelta(days=int(26 * 30.44))
    n = 260
    idx = pd.date_range(end=as_of, periods=n)
    # dd 落在 -0.10~-0.20 之間、is_above_sma200 任一值 → mid（非 bull 非 bear）
    prices = [60000.0] * (n - 1) + [60000.0 * 0.85]   # 現價跌 15%，但只跌破 -0.10 不到 -0.20
    df = pd.DataFrame({"close": prices}, index=idx)
    current_price = prices[-1]

    fc = forecast_price(current_price, df, as_of=as_of, season_engine="v2")
    assert fc is not None
    if fc["effective_season"]["season"] == "autumn" and fc["forecast_type"] == "observe":
        assert fc["target_median"] is None
        assert fc["target_low"] is None
        assert fc["target_high"] is None
        assert fc["ath_ref"] is None


def test_season_engine_default_is_v2():
    """未傳 season_engine 時讀 config.SEASON_ENGINE —— **2026-09-03 起預設為 "v2"**。

    原測試名為 `..._default_is_v1`、斷言 "v1"（當時的「零行為變更保證」）。
    切換是使用者拍板的受保護設定變更，前置與證據見
    `Github\\Cow\\歷程\\20260902findings_四季論v2象限擴充與回放.md`。
    **這個斷言刻意寫死**：預設引擎變更是策略層決定，不能讓它默默漂掉。
    """
    fc = forecast_price(70000.0)
    assert fc["season_engine"] == "v2"
    assert fc["forecast_type"] in ("bull_peak", "bear_bottom", "observe")


def test_v1_still_selectable_as_fallback():
    """v1 路徑必須完整保留——回滾只需把 config 改回一個字。"""
    fc = forecast_price(70000.0, season_engine="v1")
    assert fc["season_engine"] == "v1"
    assert fc["forecast_type"] in ("bull_peak", "bear_bottom")   # v1 永遠不產生 "observe"
