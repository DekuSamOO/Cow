"""scripts/stock_profile.py 純函數測試（合成資料、零網路、零 DB）。

只測「會靜默給出錯誤數字」的那幾件事：金額量級、還原基準一致性、成交額分位、
分級門檻邊界。網路與 climber DB 那層由呼叫端自然重試，不在此模擬。
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from stock_profile import (_coverage, _dims, _fmt_money, _low_position_note,   # noqa: E402
                           _margin_chg, _momentum_block, render, short_term_traits)
from core.momentum import time_series_momentum                                 # noqa: E402


def _df(n=300, close=100.0, vol=1_000_000, spread=0.02):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    c = pd.Series([close] * n, index=idx, dtype=float)
    return pd.DataFrame({
        "open": c, "high": c * (1 + spread), "low": c * (1 - spread),
        "close": c, "volume": [float(vol)] * n,
    }, index=idx)


# ── 金額量級（會直接讓人讀錯 3 個數量級的那種錯）─────────────────────────────

def test_fmt_money_has_trillion_tier_for_twd():
    """2330 市值 6.2e13，只到「億」會印成 621,080 億元——量級瞬間讀不出來。"""
    assert _fmt_money(6.21e13, "TWD") == "62.10 兆元"
    assert _fmt_money(1.3356e10, "TWD") == "133.56 億元"
    assert _fmt_money(6.81e7, "TWD") == "6,810 萬元"
    assert _fmt_money(None, "TWD") == "—"


def test_fmt_money_usd_scales_to_billions():
    assert _fmt_money(2.629e10, "USD") == "$26.29B"
    assert _fmt_money(5.5e6, "USD") == "$5.5M"


# ── 短線特性 ────────────────────────────────────────────────────────────────

def test_turnover_pctile_uses_dollars_not_shares():
    """跨年代比較必須用成交額：股價長期上漲時，股數會萎縮而金額成長。
    NVDA 實例：股數分位 0、成交額分位 79（同期股價漲 115 倍）。

    合成同構情境用**幾何**序列（價格 ×16、股數 ÷16，兩者相乘恆定）——成交金額全程
    一模一樣、活躍度完全沒變，但股數分位會說「史上最低」。這正是股數分位的失真模式。
    （刻意不用線性序列：線性價 × 線性量是開口向下的拋物線，末端本來就落在低分位，
    那樣測到的是拋物線不是本函式。）"""
    n = 300
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    step = np.arange(n) / (n - 1)
    price = pd.Series(10 * 16 ** step, index=idx)
    vol = pd.Series(1.6e7 * (1 / 16) ** step, index=idx)
    df = pd.DataFrame({"open": price, "high": price * 1.02, "low": price * 0.98,
                       "close": price, "volume": vol}, index=idx)
    st = short_term_traits(df, is_tw=False)
    assert (price * vol).std() / (price * vol).mean() < 1e-9   # 成交額確實恆定
    assert st["vol_pctile"] < 0.05             # 股數：史上最低，看起來像沒人交易
    # 金額：完全沒變 → 分位落在中段。不精算 0.5——浮點讓「恆定」的乘積沒有精確 ties，
    # midrank 因此落在 0.47 附近；斷言區間才是這個測試真正要說的事。
    assert 0.40 < st["turnover_pctile"] < 0.60
    assert st["turnover_pctile"] > st["vol_pctile"]


def test_liquidity_tier_thresholds_by_market():
    """分級門檻是市場慣例（台股億元／美股 5000 萬美元），兩市場不可共用同一組。"""
    tw_thick = short_term_traits(_df(close=100, vol=2_000_000), is_tw=True)   # 2 億
    tw_thin = short_term_traits(_df(close=10, vol=50_000), is_tw=True)        # 50 萬
    assert tw_thick["liquidity_tier"] == "充裕"
    assert tw_thin["liquidity_tier"] == "偏薄"
    assert tw_thick["turnover_unit"] == "TWD"
    # 同一筆 2 億「美元」才算美股充裕；2 億台幣等值的量在美股門檻下是中等
    us = short_term_traits(_df(close=100, vol=200_000), is_tw=False)          # $2000 萬
    assert us["liquidity_tier"] == "中等" and us["turnover_unit"] == "USD"


def test_limit_move_days_is_tw_only():
    """±10% 漲跌停是台股制度，美股沒有 → 該欄必須是 None 而不是 0
    （0 會被讀成「美股沒出現過極端日」，那是假資訊）。"""
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    c = pd.Series([100.0] * (n - 1) + [112.0], index=idx)      # 末根 +12%
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c,
                       "volume": [1e6] * n}, index=idx)
    assert short_term_traits(df, is_tw=True)["limit_move_days_60"] == 1
    assert short_term_traits(df, is_tw=False)["limit_move_days_60"] is None


def test_turnover_rate_needs_shares_outstanding():
    """已發行股數取不到時回 None，不得用任何替代值硬算週轉率。"""
    df = _df(vol=1_000_000)
    assert short_term_traits(df, is_tw=True, shares_out=None)["turnover_rate_pct"] is None
    got = short_term_traits(df, is_tw=True, shares_out=100_000_000)["turnover_rate_pct"]
    assert got == pytest.approx(1.0)


def test_gap_ratio_and_amplitude_measure_different_things():
    """盤中振幅（high−low）與隔夜跳空是兩件事，報表把兩者並列就是要讓人看出
    「波動發生在哪裡」。**ATR 不在本檔的責任範圍**——它由 core/indicators 產出、
    本檔只讀欄位（見 `test_atr_reads_precomputed_column_not_a_second_formula`），
    所以這裡不再對 ATR 斷言，那是 core 的測試該管的事。"""
    flat = _df(spread=0.02)                       # high/low ±2%、無跳空
    st = short_term_traits(flat, is_tw=True)
    assert st["amp_median_pct"] == pytest.approx(4.0, rel=0.01)   # (1.02−0.98)/1.00
    assert st["gap_over_2pct_ratio"] == 0.0

    gapped = flat.copy()
    gapped.iloc[-1, gapped.columns.get_loc("open")] = 110.0    # 末根跳空 +10%
    st2 = short_term_traits(gapped, is_tw=True)
    assert st2["gap_over_2pct_ratio"] > 0
    assert st2["amp_median_pct"] == st["amp_median_pct"]        # 跳空不影響盤中振幅


# ── 融資變化 ────────────────────────────────────────────────────────────────

def test_margin_chg_absent_column_returns_none():
    """美股 df 沒有 Margin_Balance 欄 → None，不得回 0（0 會被讀成「融資沒變動」）。"""
    assert _margin_chg(_df()) is None


def test_margin_chg_is_daily_not_multi_day():
    """必須是**日**變化：抄底 leverage 是滿分 40 的最強維，門檻 −1/−3/−5% 在日變化上校準
    （正本 `tw_calib_extract` 用 `Margin_Balance.pct_change()`）。餵別的口徑進去＝拿別的尺
    去量校準好的門檻。"""
    df = _df(n=20)
    df["Margin_Balance"] = [1000.0] * 19 + [900.0]      # 只有最後一天 −10%
    assert _margin_chg(df) == pytest.approx(-10.0)
    df2 = _df(n=20)
    df2["Margin_Balance"] = list(range(1000, 1020))      # 每日 +0.1% 左右
    got = _margin_chg(df2)
    assert got == pytest.approx((1019 / 1018 - 1) * 100)  # 只看最後兩天，非累積


def test_margin_chg_returns_none_across_data_gap():
    """`dropna()` 會把資料斷層吃掉：climber 的 Margin_Balance 自 2026-07-10 起整段斷掉、
    只剩 07-31 孤立一筆，舊式 `iloc[-1]/iloc[-6]` 於是跨了 **28 天**還自稱「近 5 日」，
    2330 得 −15.13%、6782 −5.49% → 兩檔都拿 leverage 滿分 40 被標「斷頭清洗」，
    直接撐起抄底 53／76 分。跨斷層寧可整維無資料，也不要靜默餵錯尺度的數字。"""
    idx = pd.to_datetime(["2026-07-08", "2026-07-09", "2026-07-31"])
    gapped = pd.DataFrame({"Margin_Balance": [34509.0, 34000.0, 29289.0]}, index=idx)
    assert _margin_chg(gapped) is None                   # 最後兩筆相隔 22 天 → 不採用
    ok = pd.DataFrame({"Margin_Balance": [34509.0, 34000.0]},
                      index=pd.to_datetime(["2026-07-08", "2026-07-09"]))
    assert _margin_chg(ok) == pytest.approx((34000 / 34509 - 1) * 100)
    # 連假容忍：週五→週一相隔 3 天仍算相鄰
    holiday = pd.DataFrame({"Margin_Balance": [100.0, 99.0]},
                           index=pd.to_datetime(["2026-07-10", "2026-07-13"]))
    assert holiday is not None and _margin_chg(holiday) == pytest.approx(-1.0)


def test_atr_reads_precomputed_column_not_a_second_formula():
    """ATR 必須讀 calculate_technical_indicators 算好的欄（pandas-ta Wilder RMA），
    不可自算 SMA——`core/risk.py` 檔頭正是為了「避免公式在兩邊分別維護後漂移」而存在，
    同一支股票在 watcher 與本檔印出兩個不同的 ATR 就是那個漂移。"""
    df = _df(n=60)
    assert short_term_traits(df, is_tw=True)["atr14_pct"] is None   # 無 ATR 欄 → None，不自算
    df2 = _df(n=60)
    df2["ATR"] = 5.0
    assert short_term_traits(df2, is_tw=True)["atr14_pct"] == pytest.approx(5.0)


# ── 2026-08-12 stock-evaluator 驗收抓到的缺陷 ──────────────────────────────

def test_render_labels_population_from_tech_df_not_yahoo():
    """分位母體必須標 tech_from/tech_bars（分位真的算在 tech_df 上），不可用 history_from。
    驗收實況：台股 tech_df 來自 climber 400 根（≈1.7 年），Yahoo 是 2,431 根，
    render 卻印「母體＝2016 起全史」——與這個 session 開場修掉的 bug 同一個病。"""
    p = _fake_profile(history_from="2016-08-12", bars=2431,
                      tech_from="2024-12-12", tech_bars=400)
    out = render(p)
    assert "母體＝2024-12-12 起 400 根" in out
    active_line = out.split("活躍度")[1].splitlines()[0]
    assert "2016-08-12" not in active_line and "2,431" not in active_line


def test_render_has_no_unconditional_gap_verdict():
    """舊版有一句無條件樣板「ATR 明顯較大＝波動主要發生在開盤那一跳」，不看任何數字、
    對跳空僅 5% 的股票也照印＝用固定文字冒充判讀。刪掉後不得再出現。"""
    out = render(_fake_profile())
    assert "波動主要發生在開盤" not in out
    assert "不可相減" in out                       # 改成講清楚兩者口徑不同


def test_coverage_flags_sparse_history():
    """「X 起 N 根」字面為真卻暗示連續性：climber 的 6782 首列 2021-01-13、893 根，
    看起來像 5.6 年，實際 2021 只有 1 列、2022 只有 25 列，2023 才完整（涵蓋率 66%）。
    低涵蓋率不影響分位算得對不對，但會讓母體標示騙人 → 必須在畫面警示。"""
    idx_full = pd.date_range("2021-01-13", "2026-08-11", freq="B")      # 連續營業日
    assert _coverage(pd.DataFrame(index=idx_full), is_tw=True) > 0.95
    assert _coverage(pd.DataFrame(index=idx_full[::2]), is_tw=True) < 0.6   # 只留一半
    assert _coverage(pd.DataFrame(index=idx_full[:20]), is_tw=True) is None   # 區間太短不猜

    out = render(_fake_profile(tech_coverage=0.66))
    assert "涵蓋率僅 66%" in out
    assert "涵蓋率" not in render(_fake_profile(tech_coverage=0.99))


def test_render_shows_three_data_cutoffs_not_just_clock():
    """只標執行時鐘會產生假新鮮的時間戳：技術面是昨收、TDCC 更舊、現價才是即時。"""
    out = render(_fake_profile())
    head = out.split("## 基本資訊")[0]
    assert "資料截止" in head and "2026-08-11" in head and "20260807" in head
    assert "現價為即時" in head


def _fake_profile(**over):
    """render() 用的最小 profile（合成，不打網路/DB）。"""
    st = {"turnover_20d": 9.35e10, "turnover_60d": 9.49e10, "liquidity_tier": "充裕",
          "turnover_unit": "TWD", "atr14_pct": 2.89, "amp_median_pct": 1.84,
          "amp_p90_pct": 3.19, "gap_over_2pct_ratio": 0.25, "vol_pctile": 0.20,
          "vol_ratio_5_60": 0.63, "turnover_pctile": 0.94, "limit_move_days_60": 1,
          "turnover_pctile_2y": 0.68, "turnover_ratio_5_60": 0.64,
          "turnover_rate_pct": 0.15}
    p = {"symbol": "2330", "name": "台灣積體電路製造股份有限公司", "market": "台股",
         "exchange": "Taiwan", "currency": "TWD", "run_at": "2026-08-12 09:42",
         "data_as_of": "2026-08-11",
         "price": 2390.0, "chg_pct": -0.21, "hi_52w": 2535.0, "lo_52w": 1125.0,
         "pos_52w_pct": 90.0, "history_from": "2016-08-12", "bars": 2431,
         "tech_from": "2016-01-04", "tech_bars": 2573, "tech_coverage": 1.0,
         "market_cap": 6.198e13, "shares_outstanding": 25932370067,
         "tech_source": "climber DB Adj_Close（至 2026-08-11）", "short_term": st,
         "price_source": "Yahoo 即時報價",
         "patterns": {"available": True, "patterns": {"has_ma_bloom": False}, "features": {}},
         "momentum_rows": ["  過去報酬      3M +30%"],
         "trend": {"score": 26, "label": "🟢 多頭趨勢", "detail": []},
         "chip": {"as_of": "2026-08-11", "valuation": {"pe": 32.0, "pb": 10.5, "yield": 0.92},
                  "tdcc": {"as_of": "20260807", "major_pct": 84.7, "retail_pct": 8.9}},
         "radar": {"high": {"score": 43, "label": "🟡 偏熱警戒", "dims": {}},
                   "low": {"score": 40, "label": "🟡 偏冷觀察", "dims": {},
                           "position_note": None}, "coverage_note": None}}
    p.update(over)
    return p


def test_turnover_ratio_is_stationary_but_full_pctile_is_not():
    """活躍度主指標必須平穩。名目成交額長期成長（2330 分年中位 2016 38 億→2026 835 億，
    21.8x）→ expanding 全史分位讓近年天數天然落在高分位（實測全史 94 vs 近2年滾動 68，
    差 25 個百分點）。合成：成交額每年穩定成長、但**最近 60 天完全沒有變活躍**。"""
    n = 900
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    price = pd.Series(10 * 20 ** (np.arange(n) / (n - 1)), index=idx)   # 名目 20 倍
    df = pd.DataFrame({"open": price, "high": price * 1.02, "low": price * 0.98,
                       "close": price, "volume": [1e6] * n}, index=idx)
    st = short_term_traits(df, is_tw=False)
    # 全史分位**飽和**到頂——名目只要單調成長，最後一天必然接近 100 分位，與活躍度無關
    assert st["turnover_pctile"] > 0.95
    # 量比只反映「5日窗與60日窗中心相隔約 27 天的那段成長」，量級小得多（此例約 1.09）。
    # 不斷言恰為 1.0：資料本身確實還在成長，量比如實反映它才對；重點是**不會飽和**。
    assert 1.0 < st["turnover_ratio_5_60"] < 1.2
    assert st["turnover_pctile_2y"] < st["turnover_pctile"]            # 滾動窗較不受汙染


def test_low_position_note_fires_only_on_lopsided_score_at_high_position():
    """抄底位置交叉檢查下沉到引擎：leverage 佔總分過半 **且** 位置不低才示警。
    低位置的融資暴減正是該維回測到的情境，不該打擾。"""
    lopsided = (76, {"leverage": {"score": 40}})
    assert _low_position_note(lopsided, 91) is not None      # 高檔＋單維獨大 → 示警
    assert _low_position_note(lopsided, 20) is None          # 低檔 → 正是該維的情境
    assert _low_position_note((76, {"leverage": {"score": 10}}), 91) is None  # 分數分散
    assert _low_position_note(lopsided, None) is None        # 無位置資料不猜


def test_price_source_is_tracked_not_inferred():
    """來源追蹤比照 Cow service 慣例：2026-08-12 驗收時 2330 的 price 與 prev_close
    恰好同值、漲跌 0.00%，從輸出無從分辨是即時報價還是回退到收盤。"""
    out = render(_fake_profile(price_source="日線收盤（即時報價取得失敗）"))
    assert "日線收盤" in out


def test_radar_dims_are_structured_not_rendered_strings():
    """`--json` 宣稱結構化輸出，dims 卻曾是 `"20/30 背離 🔴 …；RSI ⚪ 中性(53)"` 這種
    渲染字串——消費端要拿 20 與 30 得跨 emoji 與全形分號剖字串。改為結構化 dict，
    並帶出各維的 `sub`（原始數值）。"""
    sig = {"institution": {"score": 13, "max": 20, "label": "法人 🟢 買超 +9%均量",
                           "note": "三大法人買賣超/均量〔弱 AUC 0.542〕",
                           "sub": {"total_net": 3691144.0, "ratio_pct": 9.2347792}}}
    d = _dims(sig)["institution"]
    assert d["score"] == 13 and d["max"] == 20
    # 法人正規化比率原本只活在渲染字串的「+9%」裡，與融資維的 fin_chg_pct 數值欄不對稱；
    # 帶出 sub 之後有全精度值，可回溯、可跨日程式化比較
    assert d["sub"]["ratio_pct"] == pytest.approx(9.2347792)
    assert _dims({"x": {"score": 1, "max": 2, "label": "L"}})["x"]["sub"] == {}   # 無 sub 不爆


def test_momentum_json_omits_backtest_rejected_stance():
    """`momentum` 只給 rets，**刻意不帶 `stance`/`label`**：TSM 當訊號在 BTC 上已回測否決
    （無預測力、打不贏 B&H），`momentum_ref_rows` 本來就刻意不掛燈號以免被讀成交易訊號。
    JSON 若把 stance 放回去，等於從後門把否決掉的東西送到消費端手上。

    ⚠️ 舊版此測試 monkeypatch 掉 `time_series_momentum` 後**自己組一個 dict 再對它斷言**，
    完全沒執行到 stock_profile 的任何一行——`momentum` 真把 stance 加回去它照樣綠燈，
    是假證據。改成真的呼叫 `_momentum_block`，並先確認引擎本身確實有 stance/label
    （否則「輸出沒有 stance」可能只是因為引擎根本沒產生它，測不到剝除這個動作）。"""
    n = 400
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = pd.Series(np.linspace(100.0, 200.0, n), index=idx)
    df = pd.DataFrame({"close": close}, index=idx)

    raw = time_series_momentum(df)
    assert raw["stance"] == "up" and raw["label"]        # 引擎確實有燈號可被誤用
    assert set(raw["rets"]) == {90, 180, 365}

    m = _momentum_block(df)
    assert set(m) == {"returns_pct", "note"}             # 只有這兩把鑰匙出得去
    assert "stance" not in m and "label" not in m
    assert m["returns_pct"] == {lb: round(r * 100, 2) for lb, r in raw["rets"].items()}


def test_render_reads_structured_dims():
    """render 改吃結構化 dims 後仍印出「分數/上限 標籤」的原樣式。"""
    p = _fake_profile()
    p["radar"]["high"]["dims"] = {"volume": {"score": 6, "max": 18,
                                             "label": "量能 🟡 偏高 73分位",
                                             "note": None, "sub": {}}}
    assert "volume：6/18 量能 🟡 偏高 73分位" in render(p)
