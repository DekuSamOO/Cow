"""逃頂警報分級/去重/遲滯 與 分數 Δ 的狀態機測試（不實際發送 LINE）。"""
import sys
import os
import json
import importlib.util
from datetime import date, datetime, timezone, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import pytest
from service.notification.builders import escape_alert_tier


def _load_notify_module():
    spec = importlib.util.spec_from_file_location(
        "daily_line_notify", os.path.join(_REPO, "scripts", "daily_line_notify.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def notify():
    return _load_notify_module()


def test_escape_alert_tier_mapping():
    """
    分級對應 — **從 config 推導、不寫死數字**。
    2026-08-25 門檻由 85/75/60 改成 51/49/45（原三級都在實測上限 55 之上＝永遠不觸發），
    這批測試當時整批紅燈就是因為寫死了舊值；改成推導後，門檻再調也不會誤報失敗。
    """
    from config import ESCAPE_ALERT_TIERS
    tiers = sorted(ESCAPE_ALERT_TIERS)          # 由低到高
    lowest, low_name = tiers[0]
    assert escape_alert_tier(lowest - 1) == (0, None)
    for floor, name in tiers:
        assert escape_alert_tier(floor) == (floor, name)
    top_floor, top_name = tiers[-1]
    assert escape_alert_tier(top_floor + 50) == (top_floor, top_name)
    # 每一級都必須落在歷史實測上限（55 分）之內，否則就是永遠觸發不了的死檔位
    assert top_floor <= 55


@pytest.fixture
def patched_state(notify, tmp_path, monkeypatch):
    """state 檔導到 tmp，攔截 LINE 發送，凍結 builders 不依賴完整 data。"""
    state_file = tmp_path / "escape_alert_state.json"
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(state_file))
    sent = []
    import service.notification.core as core
    monkeypatch.setattr(core, "_send_line_message", lambda msgs: sent.append(msgs))
    import service.notification.builders as builders
    monkeypatch.setattr(builders, "build_escape_alert_flex", lambda s: {"type": "flex"})
    return state_file, sent


def _data(score):
    return {"escape_score": score, "escape_signals": {"derivatives": {"score": 1, "max": 30}}}


def test_escape_alert_below_threshold_no_push(notify, patched_state):
    state_file, sent = patched_state
    notify.maybe_send_escape_alert(_data(40))
    assert sent == []
    assert not state_file.exists() or "last_escape_score" not in json.loads(state_file.read_text())


def test_escape_alert_first_cross_pushes(notify, patched_state):
    from config import ESCAPE_ALERT_TIERS
    lowest = min(f for f, _ in ESCAPE_ALERT_TIERS)
    state_file, sent = patched_state
    notify.maybe_send_escape_alert(_data(lowest))
    assert len(sent) == 1
    st = json.loads(state_file.read_text())
    assert st["last_escape_score"] == lowest and st["last_escape_tier"] == lowest


def test_escape_alert_same_day_dedupe(notify, patched_state):
    _, sent = patched_state
    notify.maybe_send_escape_alert(_data(62))
    notify.maybe_send_escape_alert(_data(70))  # 同日即使 +8 也不再推
    assert len(sent) == 1


def test_escape_alert_cross_day_needs_delta_or_upgrade(notify, patched_state):
    """跨日同級：需 >= REPUSH_DELTA 才再推；升級則無論差值都推。門檻與 delta 皆由 config 推導。"""
    from config import ESCAPE_ALERT_TIERS, ESCAPE_ALERT_REPUSH_DELTA as DELTA
    tiers = sorted(ESCAPE_ALERT_TIERS)
    low, mid = tiers[0][0], tiers[1][0]
    # delta 必須 <= 最小級距間隔，否則「同級再推」永遠用不到（死規則）
    gaps = [b[0] - a[0] for a, b in zip(tiers, tiers[1:])]
    assert DELTA <= min(gaps), f"REPUSH_DELTA={DELTA} 大於最小級距間隔 {min(gaps)}"

    state_file, sent = patched_state
    state_file.write_text(json.dumps(
        {"last_escape_date": "2020-01-01", "last_escape_score": low, "last_escape_tier": low}))
    notify.maybe_send_escape_alert(_data(low + DELTA - 1))     # 差值不足且未升級 → 不推
    assert len(sent) == 0
    notify.maybe_send_escape_alert(_data(low + DELTA))         # 達 delta → 推
    assert len(sent) == 1
    # 同級內差值不足、但升級 → 仍要推
    state_file.write_text(json.dumps(
        {"last_escape_date": "2020-01-01", "last_escape_score": mid - 1,
         "last_escape_tier": low}))
    notify.maybe_send_escape_alert(_data(mid))
    assert len(sent) == 2


def test_escape_alert_disarm_on_drop(notify, patched_state):
    state_file, sent = patched_state
    state_file.write_text(json.dumps(
        {"last_escape_date": "2020-01-01", "last_escape_score": 70, "last_escape_tier": 60}))
    notify.maybe_send_escape_alert(_data(30))  # 跌回門檻下 → 解除武裝
    st = json.loads(state_file.read_text())
    assert "last_escape_score" not in st
    notify.maybe_send_escape_alert(_data(61))  # 重新跨門檻 → 新事件，直接推
    assert len(sent) == 1


def test_attach_score_deltas(notify, patched_state):
    state_file, _ = patched_state
    # 前一日分數 50/40 → 今日 62/35 → Δ +12/-5
    # 基準日**必須相對今天算**：2026-09-11 起 Δ 有新鮮度守門（見
    # tests/test_score_history_freshness.py），舊版這裡寫死 "2020-01-01"，
    # 名義上叫「前一日」實際是六年前，守門一上線就整條不給 Δ。
    yesterday = str(date.today() - timedelta(days=1))
    state_file.write_text(json.dumps(
        {"score_history": {yesterday: {"escape": 50, "low": 40}}}))
    data = {"escape_score": 62, "low_score": 35}
    notify.attach_score_deltas(data)
    assert data["escape_delta"] == 12
    assert data["low_delta"] == -5
    hist = json.loads(state_file.read_text())["score_history"]
    assert any(v == {"escape": 62, "low": 35} for k, v in hist.items() if k != yesterday)


def test_attach_score_deltas_first_run_no_delta(notify, patched_state):
    data = {"escape_score": 62, "low_score": 35}
    notify.attach_score_deltas(data)
    assert "escape_delta" not in data and "low_delta" not in data


# ── 週報 ──────────────────────────────────────────────────────────────────────
_TW = timezone(timedelta(hours=8))
_SUN_EVE = datetime(2026, 6, 14, 18, 30, tzinfo=_TW)   # 週日 18:30


def _weekly_data():
    return {"price": "$100,000", "week_change_pct": 3.2, "week_high": 105000.0,
            "week_low": 98000.0, "trend_level": "🟢 多頭趨勢",
            "composite_action": "順勢持有", "composite_pos": "建議倉位 60–80%（未擬合）"}


def _flex_text(msg):
    """攤平 Flex 訊息所有 text 欄位 + altText 成單一字串，供子字串斷言。"""
    out = [msg.get("altText", "")]

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "text" and "text" in o:
                out.append(o["text"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(msg)
    return " ".join(out)


def test_weekly_summary_only_sunday_evening(notify, patched_state):
    _, sent = patched_state
    notify.maybe_send_weekly_summary(_weekly_data(), now=datetime(2026, 6, 10, 18, 30, tzinfo=_TW))  # 週三
    notify.maybe_send_weekly_summary(_weekly_data(), now=datetime(2026, 6, 14, 8, 30, tzinfo=_TW))   # 週日早上
    assert sent == []
    notify.maybe_send_weekly_summary(_weekly_data(), now=_SUN_EVE)
    assert len(sent) == 1
    msg = sent[0][0]
    assert msg["type"] == "flex"          # 已改 Flex Message（非純文字）
    text = _flex_text(msg)
    assert "BTC 週報" in text and "+3.2%" in text and "今日行動" not in text


def test_weekly_summary_dedupe_and_scores(notify, patched_state):
    state_file, sent = patched_state
    import json as _json
    state_file.write_text(_json.dumps({"score_history": {
        "2026-06-12": {"escape": 30, "low": 50},
        "2026-06-13": {"escape": 45, "low": 40},
        "2026-06-14": {"escape": 38, "low": 42},
    }}))
    notify.maybe_send_weekly_summary(_weekly_data(), now=_SUN_EVE)
    assert len(sent) == 1
    msg = sent[0][0]
    assert msg["type"] == "flex"
    text = _flex_text(msg)
    # 逃頂分週高/低（max 45 / min 30）與抄底分週高/低（max 50 / min 40）入卡
    assert "逃頂分" in text and "45" in text and "30" in text
    assert "抄底分" in text and "50" in text
    # 同日再呼叫 → 去重
    notify.maybe_send_weekly_summary(_weekly_data(), now=_SUN_EVE)
    assert len(sent) == 1


def test_weekly_summary_insufficient_data_skipped(notify, patched_state):
    _, sent = patched_state
    notify.maybe_send_weekly_summary({}, now=_SUN_EVE)   # 無價格也無分數史
    assert sent == []


# ── 資料缺值不得靜默（2026-09-07 立）──────────────────────────────────────
# 立規原因＝P4 馬丁重啟哨兵的死法：取不到行情就 print("略過") 收工，於是
# 「該響卻響不了」與「偵測到沒事」長得一模一樣，靜默兩週沒人發現，最後整組移除。
# 三條紅線：①缺值必須推警示 ②同一次故障不得洗版 ③資料恢復必須清旗標。

def _gap_flag(state_file, key):
    return json.loads(state_file.read_text()).get(f"datagap_{key}")


def test_leverage_data_gap_is_not_silent(notify, patched_state):
    """升槓桿哨兵缺 AHR999／距 ATH → 必須推警示，不可只 print 略過。"""
    state_file, sent = patched_state
    notify.maybe_send_leverage_window_alert({"ahr999": None, "days_since_ath": None})
    assert len(sent) == 1, "資料缺值時哨兵靜默＝P4 的死法重演"
    text = sent[0][0]["text"]
    assert "哨兵失明" in text and "升槓桿窗口哨兵" in text
    assert "偵測不了" in text, "必須說清楚這不是『偵測到沒事』"
    assert _gap_flag(state_file, "leverage") is True


def test_data_gap_alert_does_not_spam(notify, patched_state):
    """一天三場＋連續故障數週，同一次故障只推一次。"""
    _, sent = patched_state
    for _ in range(9):                      # 三天 × 每天三場
        notify.maybe_send_leverage_window_alert({"ahr999": None, "days_since_ath": None})
    assert len(sent) == 1


def test_data_gap_flag_cleared_when_data_returns(notify, patched_state):
    """資料恢復 → 清旗標；下次再故障要能重新提醒（不可推過一次就永久靜音）。"""
    state_file, sent = patched_state
    notify.maybe_send_leverage_window_alert({"ahr999": None, "days_since_ath": None})
    assert len(sent) == 1
    # 資料恢復（窗口未開，不會有其他推播）
    notify.maybe_send_leverage_window_alert({"ahr999": 0.53, "days_since_ath": 336,
                                             "current_price": 79_800.0})
    assert _gap_flag(state_file, "leverage") is None, "資料恢復必須清旗標"
    # 再故障 → 重新提醒
    notify.maybe_send_leverage_window_alert({"ahr999": None, "days_since_ath": None})
    assert len(sent) == 2


def test_hedge_rsi_gap_is_not_silent(notify, patched_state):
    """套保哨兵缺 RSI → 同樣不得靜默（2026-08-25~09-02 曾因此靜默 8 天）。"""
    state_file, sent = patched_state
    notify.maybe_send_hedge_batch_alert({"rsi14_closed": None, "rsi_peak": None})
    assert len(sent) == 1 and "套保建倉哨兵" in sent[0][0]["text"]
    assert _gap_flag(state_file, "hedge") is True


def test_d3_data_gap_is_not_silent(notify, patched_state):
    """熊底 D3 哨兵缺低點／現價 → 不得靜默。"""
    state_file, sent = patched_state
    notify.maybe_send_bear_bottom_confirm_alert({"bear_low_since_ath": None,
                                                 "current_price": None})
    assert len(sent) == 1 and "熊底確認 D3 哨兵" in sent[0][0]["text"]
    assert _gap_flag(state_file, "d3") is True


def test_advisory_sentinels_stay_quiet_on_missing_data(notify, patched_state):
    """刻意的反面：逃頂／合成行動**不**納入缺值告警（SOP F-4 已定為不作賣出依據）。

    這條守的是「別把雜訊也一起推上來」——四個會影響下單的哨兵才推缺值警示。
    """
    _, sent = patched_state
    notify.maybe_send_action_alert({})            # 無 composite_action
    assert sent == []
