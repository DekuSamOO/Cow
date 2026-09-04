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
    find_bear_low, WINDOW_RESET_DAYS, d3_grid_plan,
    BEAR_DRAWDOWN, D3_TRIGGER_DRAWDOWN,
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


def test_d3_grid_plan_bounds_and_liq_estimate():
    """下限=前波新低本身；上限=下限 x step^count；強平估算=開單價 x liq_mult。"""
    plan = d3_grid_plan(lower_bound=50_000.0, open_price=80_000.0,
                         grid_step=1.0074, grid_count=100, liq_mult=0.667)
    assert plan["lower"] == 50_000.0
    assert abs(plan["upper"] - 50_000.0 * (1.0074 ** 100)) < 1e-6
    assert plan["grid_count"] == 100
    assert abs(plan["liq_estimate"] - 80_000.0 * 0.667) < 1e-6
    # 上限必須高於下限（100 格、每格漲 0.74% 是正報酬率）
    assert plan["upper"] > plan["lower"]


def test_d3_grid_plan_missing_inputs_returns_none():
    assert d3_grid_plan(None, 80_000.0, 1.0074, 100, 0.667) is None
    assert d3_grid_plan(50_000.0, None, 1.0074, 100, 0.667) is None


# ── D3 死結偵測 + c3 門檻拆參數（2026-09-04）──────────────────────────────
# 背景：c3 原本重用 BEAR_DRAWDOWN(30%)，造成 c1 與 c3 互斥的死結。
# 已拆成獨立的 D3_TRIGGER_DRAWDOWN(20%)，find_bear_low 仍用 BEAR_DRAWDOWN(30%)。
# 紅線：
#   1. 互斥時必須偵測到（不可靜默）
#   2. 新預設值必須解除 2026-09 的實況死結
#   3. 放寬門檻**不得**放行 2021-10-18 誤報
#   4. 偵測與調參**不得**改變 c1/c2 的判定邏輯
#   5. 兩個門檻必須是獨立參數（改 c3 不可動到找低點的定義）

_ATH_2026, _LOW_2026 = 124_658.54, 58_538.0      # 2026-09-04 實況


def test_d3_deadlock_detected_when_gates_mutually_exclusive():
    """舊值 30% 下，2026-09 實況是死結 —— 這就是原本沒被說出來的問題。"""
    r = d3_status(80_986.0, _LOW_2026, "2026-06-30", 66,
                  cycle_ath=_ATH_2026, trigger_drawdown=0.30)
    assert r["deadlock"] is True, "互斥卻沒偵測到 —— 這就是原本的靜默死結"
    assert r["price_req"] > _ATH_2026 * 0.70          # c1 下限 > c3 上限
    assert abs(r["deadlock_max_c3"] - (1 - 1.5 * _LOW_2026 / _ATH_2026)) < 1e-12
    assert 0.294 < r["deadlock_max_c3"] < 0.296      # ≈29.56%


def test_d3_new_default_resolves_the_2026_deadlock():
    """紅線 2：改用 D3_TRIGGER_DRAWDOWN(20%) 之後，同一組資料必須不再死結。"""
    r = d3_status(80_986.0, _LOW_2026, "2026-06-30", 66, cycle_ath=_ATH_2026)
    assert D3_TRIGGER_DRAWDOWN == 0.20
    assert r["deadlock"] is False, "新門檻沒解除死結，等於這次調整白做"
    # 可行區間非空：c1 下限 87,807 <= c3 上限 99,727
    assert r["price_req"] < _ATH_2026 * (1 - D3_TRIGGER_DRAWDOWN)
    # 但今天仍未觸發（c1/c2 都還沒到）—— 調門檻不等於馬上進場
    assert r["ok"] is False and r["c1"] is False and r["c2"] is False


def test_d3_misfire_2021_10_18_still_blocked_at_new_threshold():
    """紅線 3：放寬到 20% 後，2021-10-18 那次誤報仍必須被 c3 擋掉。

    當時：price 62,010、cycle ATH ≈63,600（距 ATH −2.5%）、低點 29,800。
    c1（+108%）與 c2 都成立，**只有 c3 能擋**——擋不住就會在大頂前一個月全押。
    """
    r = d3_status(62_010.0, 29_800.0, "2021-07-20", 90, cycle_ath=63_600.0)
    assert r["c1"] is True and r["c2"] is True, "前提：這次誤報 c1/c2 本來就會過"
    assert r["c3"] is False, "20% 門檻放行了 2021-10-18 誤報 —— 絕不可接受"
    assert r["ok"] is False


def test_d3_deadlock_clears_when_c3_relaxed():
    """同一組低點/ATH，門檻放寬就有解 —— 證明死結來自門檻不是資料。"""
    assert d3_status(80_986.0, _LOW_2026, "2026-06-30", 66,
                     cycle_ath=_ATH_2026, trigger_drawdown=0.30)["deadlock"] is True
    r25 = d3_status(80_986.0, _LOW_2026, "2026-06-30", 66,
                    cycle_ath=_ATH_2026, trigger_drawdown=0.25)
    assert r25["deadlock"] is False
    assert r25["price_req"] < _ATH_2026 * 0.75


def test_d3_deadlock_false_when_bear_is_deep_enough():
    """夠深的熊市（低點離 ATH 夠遠）不該被誤報成死結。"""
    r = d3_status(45_000.0, 30_000.0, "2026-01-01", 120, cycle_ath=100_000.0)
    assert r["deadlock"] is False
    assert r["ok"] is True                    # 且此時 D3 本來就該成立


def test_d3_deadlock_is_none_without_cycle_ath():
    """沒傳 cycle_ath 就沒有 c3，也就談不上死結 —— 必須回 None 不是 False。"""
    r = d3_status(160.0, 100.0, "2026-01-01", 120)
    assert r["deadlock"] is None and r["deadlock_max_c3"] is None


def test_d3_thresholds_are_separate_parameters():
    """紅線 5：c3 與「找低點」必須是兩個獨立參數，改 c3 不可動到 find_bear_low。"""
    assert BEAR_DRAWDOWN == 0.30 and D3_TRIGGER_DRAWDOWN == 0.20
    assert BEAR_DRAWDOWN != D3_TRIGGER_DRAWDOWN, "拆參數的意義就在兩者可以不同"
    # find_bear_low 仍以 30% 為準：ATH 100 時，71 不算低點、69 才算
    assert find_bear_low([100.0, 71.0], 100.0) == (None, None)
    assert find_bear_low([100.0, 69.0], 100.0)[0] == 69.0


def test_d3_deadlock_does_not_change_verdict():
    """紅線 4：加了偵測之後，ok/c1/c2/c3 的判定必須與獨立算式一致。"""
    for px, days in ((80_986.0, 66), (95_000.0, 200), (60_000.0, 10)):
        r = d3_status(px, _LOW_2026, "2026-06-30", days, cycle_ath=_ATH_2026)
        # 用獨立算式重算一次，不重用 d3_status 的中間值
        exp_c1 = (px / _LOW_2026 - 1) >= 0.50
        exp_c2 = days >= 90
        exp_c3 = (px / _ATH_2026 - 1) <= -D3_TRIGGER_DRAWDOWN
        assert r["c1"] is exp_c1 and r["c2"] is exp_c2 and r["c3"] is exp_c3
        assert r["ok"] is bool(exp_c1 and exp_c2 and exp_c3)
