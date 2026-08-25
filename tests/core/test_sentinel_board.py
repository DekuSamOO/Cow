"""哨兵總覽（2026-08-25）單元測試 — 純顯示、不得有副作用。"""
import core.sentinel_board as SB
from core.sentinel_board import sentinel_rows, HEDGE_BATCHES, HEDGE_G3_PEAK


def test_all_seven_sentinels_always_listed():
    """七個哨兵一律列出——取不到值標「—」而不是整段消失（死項要看得見）。"""
    rows = sentinel_rows(state={})
    assert len(rows) == 7
    for i, r in enumerate(rows, 1):
        assert r.startswith(str(i)), f"第 {i} 列順序錯：{r}"


def test_unreadable_state_is_not_reported_as_no_record(tmp_path, monkeypatch):
    """
    **狀態讀不到 ≠ 沒發生過。**
    2026-08-25 實測：`escape_alert_state.json` 只存在於 GH Actions artifact，本機沒有
    → 面板把「已推播過」的哨兵印成「尚無紀錄」（實際 last_action_label=順勢持有）。
    這是同一類「顯示得出來、內容永遠是空的」問題，必須在文案上分得出來。
    """
    monkeypatch.setattr(SB, "STATE_FILE", str(tmp_path / "no.json"))
    monkeypatch.setattr(SB, "REMOTE_CACHE", str(tmp_path / "no2.json"))
    rows = sentinel_rows()                       # state=None → 走 load_state
    for i in (1, 2, 6):                          # 2/3/7 列（index 1,2,6）
        assert "尚無紀錄" not in rows[i], f"讀不到狀態卻宣稱沒紀錄：{rows[i]}"
        assert "讀不到" in rows[i]
    assert "狀態未知" in rows[4]                  # D3 不得說「待命」
    assert "已建 ?/3" in rows[5]                  # 套保批次不得說 0/3
    assert any("不可信" in r for r in rows)


def test_no_record_still_says_no_record_when_state_is_readable():
    """有狀態、只是該鍵沒紀錄 → 仍要說「尚無紀錄」，不可一律推給讀不到。"""
    rows = sentinel_rows(state={"last_action_label": "順勢持有"})
    assert "順勢持有" in rows[1]
    assert "尚無紀錄" in rows[2]      # 馬丁重啟真的沒紀錄


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
    monkeypatch.setattr(SB, "REMOTE_CACHE", str(tmp_path / "nope.json"))
    st, source = SB.load_state()
    assert st == {} and source == "unavailable"


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


# ── 版面約束（2026-08-25）──────────────────────────────────────────────────────
# 教訓：完整版 10 列加進 BTC 儀表板時只驗了內容、沒驗版面。實測儀表板**改動前就已 51 列**，
# 加完變 61 列；橫向沒撐開純屬運氣（兩欄區把 W 壓在 102，哨兵最寬才 62）。
def test_compact_view_is_two_lines():
    """儀表板用的壓縮版必須恰好兩行——垂直空間是這個畫面最稀缺的資源。"""
    from core.sentinel_board import sentinel_compact
    rows = sentinel_compact(top_score=14,
                            gate={"ok": False, "g1": False, "g2": True, "ahr": 0.5, "dath": 323},
                            d3={"ok": False, "c1": False, "c2": False, "c3": True,
                                "rebound": 0.35, "days": 56},
                            rsi14=81.8, rsi_max_90d=85.9, state={})
    assert len(rows) == 2, f"壓縮版必須兩行，實際 {len(rows)} 行"


def test_compact_view_fits_panel_width():
    """
    寬度不得超過兩欄版的版面下限（2*_MIN_COL_W+2），否則會把整個儀表板撐寬
    —— render 的 w_full 直接吃 quote 區每一行（BTC_WATCH.py 的 `w_full = max(...)`）。
    """
    from core.sentinel_board import sentinel_compact
    from core.term_ui import _dw, _MIN_COL_W
    limit = 2 * _MIN_COL_W + 2
    for state in ({}, {"last_action_label": "分批止盈／考慮對沖空單", "hedge_batch_1": True}):
        rows = sentinel_compact(top_score=55,
                                gate={"ok": True, "g1": True, "g2": True, "ahr": 0.39, "dath": 400},
                                d3={"ok": True, "c1": True, "c2": True, "c3": True,
                                    "rebound": 1.08, "days": 120},
                                rsi14=49.0, rsi_max_90d=86.0, state=state)
        for r in rows:
            assert _dw(r) <= limit, f"寬 {_dw(r)} > 版面下限 {limit}：{r}"


def test_full_view_still_seven_rows_for_entry_screen():
    """watcher 進場畫面仍用完整版（該頁沒有東西跟它搶垂直空間）。"""
    assert len(sentinel_rows(state={})) == 7
