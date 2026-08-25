# -*- coding: utf-8 -*-
"""
tests/test_leverage_window.py — 升槓桿窗口 / 熊底確認 D3 的守門（2026-08-25）。

守五件事：
  1. 兩道閘門的判定與缺值處理
  2. trigger_price 的二次式關係（AHR999 對價格是二次式）
  3. **分批計數以「訊號日」累計，短暫關窗不重置**——這是 2026-08-25 修正的
     核心 bug：舊版「開窗即 lev_batches_sent=1」會被 2~6 天的小反彈重置，
     2018 那段窗口被切成 7 截即重置 6 次、六批永遠投不完。
  4. find_bear_low 的 30% 跌幅門檻（LINE 哨兵與 BTC_WATCH 共用這一份）
  5. D3 兩個條件（反彈幅度、距最低天數）須同時成立
數字一律不寫進本檔真值（公開版控），全部用顯假值。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.leverage_window import (  # noqa: E402
    gate_status, trigger_price, advance_batches, d3_status, compact_rows,
    find_bear_low, WINDOW_RESET_DAYS,
)

AHR_MAX, MIN_DAYS = 0.40, 300
BATCH_DAYS, BATCH_N = 14, 6


def test_gate_status_basic():
    assert gate_status(0.30, 350, AHR_MAX, MIN_DAYS)["ok"] is True
    assert gate_status(0.50, 350, AHR_MAX, MIN_DAYS)["ok"] is False   # 閘門一不過
    assert gate_status(0.30, 200, AHR_MAX, MIN_DAYS)["ok"] is False   # 閘門二不過
    # 邊界：嚴格小於 / 大於等於
    assert gate_status(AHR_MAX, 350, AHR_MAX, MIN_DAYS)["g1"] is False
    assert gate_status(0.30, MIN_DAYS, AHR_MAX, MIN_DAYS)["g2"] is True


def test_gate_status_missing_is_undecidable():
    for a, dd in ((None, 350), (0.30, None), (None, None)):
        assert gate_status(a, dd, AHR_MAX, MIN_DAYS)["ok"] is None


def test_trigger_price_is_quadratic():
    """AHR999 = (P/SMA200)x(P/PL) 對 P 是二次式 → 門檻價 = P x sqrt(ahr_max/ahr)。"""
    p, ahr = 80_000.0, 0.64
    tp = trigger_price(p, ahr, 0.16)      # 目標為現值的 1/4 → 價格應減半
    assert abs(tp - p * 0.5) < 1e-6
    # 已在門檻之下 → 回傳值高於現價（代表「不必再跌」）
    assert trigger_price(p, 0.20, 0.40) > p
    assert trigger_price(0, 0.5, 0.4) is None
    assert trigger_price(p, None, 0.4) is None


def test_batches_advance_on_signal_days_only():
    """開窗第 1 天發第 1 批；其後每滿 BATCH_DAYS 個訊號日才發下一批。"""
    st, sent_days = {}, 0
    fired = []
    for day in range(1, 60):
        st, batch, ev = advance_batches(st, True, f"2026-09-{day:02d}", BATCH_DAYS, BATCH_N)
        sent_days += 1
        if batch:
            fired.append((sent_days, batch))
    assert fired[0] == (1, 1)
    assert fired[1] == (BATCH_DAYS, 2)          # 第 14 個訊號日
    assert fired[2] == (2 * BATCH_DAYS, 3)
    assert len(fired) <= BATCH_N


def test_short_close_does_not_reset_batches():
    """核心迴歸：窗口被兩天小反彈切斷後重開，批次計數必須延續。"""
    st = {}
    st, b1, ev1 = advance_batches(st, True, "2026-09-01", BATCH_DAYS, BATCH_N)
    assert (b1, ev1) == (1, "open")
    for d in ("2026-09-02", "2026-09-03"):      # 關窗兩天
        st, b, ev = advance_batches(st, False, d, BATCH_DAYS, BATCH_N)
    assert st["lev_batches_sent"] == 1, "關窗不得清掉已投批次"
    st, b, ev = advance_batches(st, True, "2026-09-04", BATCH_DAYS, BATCH_N)
    assert ev == "reopen" and b is None, "重開不得當成新窗口再發第 1 批"
    assert st["lev_batches_sent"] == 1
    # 續跑到累計滿 BATCH_DAYS 個訊號日才發第 2 批
    sig = st["lev_signal_days"]
    day = 5
    while st["lev_signal_days"] < BATCH_DAYS:
        st, b, ev = advance_batches(st, True, f"2026-09-{day:02d}", BATCH_DAYS, BATCH_N)
        day += 1
    assert b == 2 and st["lev_batches_sent"] == 2
    assert sig < BATCH_DAYS   # 證明中間確實有累積過程，不是一次跳到位


def test_long_close_resets():
    st = {"lev_window_open": True, "lev_signal_days": 30, "lev_batches_sent": 3}
    st, b, ev = advance_batches(st, False, "2026-10-01", BATCH_DAYS, BATCH_N)
    assert ev == "close" and st["lev_batches_sent"] == 3
    for i in range(WINDOW_RESET_DAYS + 2):
        st, b, ev = advance_batches(st, False, f"d{i}", BATCH_DAYS, BATCH_N)
    assert st["lev_batches_sent"] == 0 and st["lev_signal_days"] == 0


def test_same_day_rerun_does_not_double_count():
    st = {}
    st, _, _ = advance_batches(st, True, "2026-09-01", BATCH_DAYS, BATCH_N)
    first = st["lev_signal_days"]
    st, _, _ = advance_batches(st, True, "2026-09-01", BATCH_DAYS, BATCH_N)
    assert st["lev_signal_days"] == first, "同一天重跑不得重複計訊號日"


def test_find_bear_low_requires_deep_drawdown():
    """低點只在「自 ATH 已跌逾 30%」的區間裡找；沒跌夠深就不該給出低點。"""
    ath = 100.0
    # 只跌到 -20%：尚未跌逾門檻 → 不可判定
    assert find_bear_low([100.0, 90.0, 80.0], ath) == (None, None)
    # 跌破 70 之後的最低收盤才算數（65 雖非全序列最低點以外的干擾，仍須取 60）
    val, pos = find_bear_low([100.0, 75.0, 65.0, 60.0, 85.0], ath)
    assert (val, pos) == (60.0, 3)
    # start_pos 之前的低點不得入選（ATH 之前的熊市低點不屬於本輪）
    val, pos = find_bear_low([50.0, 100.0, 68.0], ath, start_pos=1)
    assert (val, pos) == (68.0, 2)
    # 同值取最早，與 pandas idxmin 一致
    assert find_bear_low([100.0, 60.0, 60.0], ath)[1] == 1
    assert find_bear_low([], ath) == (None, None)
    assert find_bear_low([100.0, 60.0], None) == (None, None)


def test_d3_requires_both_conditions():
    low, req = 100.0, 0.50
    assert d3_status(160.0, low, "2026-01-01", 120)["ok"] is True     # 兩條件皆過
    assert d3_status(160.0, low, "2026-01-01", 30)["ok"] is False     # 天數不足
    assert d3_status(130.0, low, "2026-01-01", 120)["ok"] is False    # 反彈不足
    r = d3_status(130.0, low, "2026-01-01", 120)
    assert abs(r["price_req"] - low * (1 + req)) < 1e-9
    assert d3_status(100.0, None, None, 0)["ok"] is None


def test_compact_rows_shape_and_width():
    """兩行、且不得超過兩欄面板決定的版面下限寬度（否則會撐寬 BTC_WATCH）。"""
    from core.term_ui import _dw, _MIN_COL_W
    limit = 2 * _MIN_COL_W + 2
    gate = gate_status(0.541, 323, AHR_MAX, MIN_DAYS)
    d3 = d3_status(80_000.0, 58_000.0, "2026-06-30", 56)
    rows = compact_rows(gate, d3, 80_000.0, AHR_MAX, MIN_DAYS)
    assert len(rows) == 2
    for r in rows:
        assert _dw(r) <= limit, f"哨兵行寬 {_dw(r)} > 版面下限 {limit}，會撐寬版面"
    # 窗口開啟時應顯示批次進度而非觸發價
    open_rows = compact_rows(gate_status(0.30, 350, AHR_MAX, MIN_DAYS), d3,
                             80_000.0, AHR_MAX, MIN_DAYS,
                             batch_days=BATCH_DAYS, sent=2, batch_count=BATCH_N,
                             signal_days=20)
    assert "窗口開啟" in open_rows[0] and "2/6" in open_rows[0]
    for r in open_rows:
        assert _dw(r) <= limit
    # D3 那行必須印出「低點的價與日期」——少了這個錨點，反彈幅度與確認價
    # 都失去參照，讀者會把確認價誤讀成「熊底應該在這個價位」（實際踩過的誤解）
    assert "58,000" in rows[1] and "06-30" in rows[1], "D3 行缺少低點錨點"
    assert "87,000" in rows[1], "D3 行缺少確認價（低點 x 1.5）"


def test_compact_rows_handles_missing():
    rows = compact_rows({"ok": None}, {"ok": None}, None, AHR_MAX, MIN_DAYS)
    assert len(rows) == 2 and "無法判定" in rows[0]
