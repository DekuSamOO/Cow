"""哨兵總覽（2026-08-25）單元測試 — 純顯示、不得有副作用。"""
import core.sentinel_board as SB
from core.sentinel_board import sentinel_rows, HEDGE_BATCHES, HEDGE_G3_PEAK


def test_all_seven_sentinels_always_listed():
    """七個哨兵一律列出——取不到值標「—」而不是整段消失（死項要看得見）。"""
    rows = sentinel_rows(state={})
    assert len(rows) == 7
    for i, r in enumerate(rows, 1):
        assert r.startswith(str(i)), f"第 {i} 列順序錯：{r}"


def test_no_side_effects_on_state_file(tmp_path, monkeypatch):
    """只讀不寫：即使狀態檔不存在也不得建立。"""
    ghost = tmp_path / "nope.json"
    monkeypatch.setattr(SB, "STATE_FILE", str(ghost))
    sentinel_rows()
    assert not ghost.exists()


def test_load_state_survives_broken_file(tmp_path, monkeypatch):
    bad = tmp_path / "s.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(SB, "STATE_FILE", str(bad))
    assert SB.load_state() == {}
    assert len(sentinel_rows()) == 7


def test_escape_tier_thresholds_are_reachable():
    """
    逃頂警報門檻必須在實測上限（55 分）之內。
    原本是 85/75/60 → 三級全部不可觸及，這個 LINE 警報等於一直關著。
    """
    from config import ESCAPE_ALERT_TIERS, ESCAPE_ALERT_THRESHOLD
    assert max(f for f, _ in ESCAPE_ALERT_TIERS) <= 55
    # 主閘門也必須可觸及，且不得高於最低級（兩處漂移＝警報整個被擋死）
    assert ESCAPE_ALERT_THRESHOLD <= min(f for f, _ in ESCAPE_ALERT_TIERS)
    row = sentinel_rows(top_score=55, state={})[0]
    assert "🔴" in row, f"實測上限 55 分仍觸發不了最高級：{row}"


def test_hedge_row_reflects_g3_precondition_and_progress():
    armed = sentinel_rows(rsi14=60.0, rsi_max_90d=86.0, state={})[5]
    assert "G3✅" in armed and "已建 0/3" in armed
    not_armed = sentinel_rows(rsi14=60.0, rsi_max_90d=70.0, state={})[5]
    assert "G3✕" in not_armed
    done = sentinel_rows(rsi14=60.0, rsi_max_90d=86.0,
                         state={"hedge_batch_1": True})[5]
    assert "已建 1/3" in done


def test_d3_row_surfaces_new_c3_gate():
    """c3（仍在熊市）未過時必須在畫面上說出來，否則使用者只會看到『沒觸發』。"""
    row = sentinel_rows(d3={"ok": False, "c1": True, "c2": True, "c3": False,
                            "rebound": 1.08, "days": 90}, state={})[4]
    assert "c3未過" in row


def test_hedge_constants_match_appendix():
    """三批門檻與規模需與執行清單附錄 E-1 一致（0.0428×2 + 0.0429 = 0.1285）。"""
    assert [thr for _, thr, _ in HEDGE_BATCHES] == [65, 55, 50]
    assert round(sum(q for _, _, q in HEDGE_BATCHES), 4) == 0.1285
    assert HEDGE_G3_PEAK == 75
