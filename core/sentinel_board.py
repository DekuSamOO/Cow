"""
core/sentinel_board.py · 哨兵總覽（單一真實來源）

2026-08-25 建立。動機：LINE 哨兵目前有 7 個，但**它們的狀態只存在推播那一刻**——
在 watcher / BTC_WATCH 畫面上完全看不到「哪些已經響過、哪些還在待命、離觸發多遠」。
本模組把 `escape_alert_state.json` 的推播狀態與當下的live 條件併成一張表。

純顯示、零副作用：**只讀狀態檔，不寫、不推播**。推播邏輯仍只在
`scripts/daily_line_notify.py`（那支才有 send_line_message）。

7 個哨兵與其去重鍵：
  1 逃頂警報      escape_alert           分級門檻 config.ESCAPE_ALERT_TIERS
  2 行動翻轉      action_alert           last_action_key
  3 馬丁重啟      mart_restart_alert     last_mart_restart_key
  4 升槓桿窗口    leverage_window_alert  兩道閘門 + 批次計數
  5 熊底確認 D3   bear_bottom_confirm    d3_confirmed
  6 套保建倉      hedge_batch_alert      hedge_batch_1/2/3
  7 週報          weekly_summary         last_weekly_date
"""
import json
import os
from typing import Optional

_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_COW, "escape_alert_state.json")

HEDGE_BATCHES = ((1, 65, 0.0428), (2, 55, 0.0428), (3, 50, 0.0429))
HEDGE_G3_PEAK = 75          # G3 前提：近 90 日 RSI 曾 > 此值


def load_state() -> dict:
    """讀推播狀態（只讀不寫）。檔案不存在／壞掉 → 回空 dict。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _mark(done: bool) -> str:
    return "✅ 已推" if done else "⏳ 待命"


def sentinel_rows(top_score: Optional[int] = None,
                  gate: Optional[dict] = None,
                  d3: Optional[dict] = None,
                  rsi14: Optional[float] = None,
                  rsi_max_90d: Optional[float] = None,
                  state: Optional[dict] = None) -> list:
    """
    回傳哨兵總覽的顯示行（list[str]）。所有參數皆選填 —— 取不到的項目照樣列出，
    標成「—」而不是整段消失（死項要看得見，這正是 2026-08-25 稽核的教訓）。
    """
    st = load_state() if state is None else state
    rows = []

    # 1 逃頂警報
    try:
        from config import ESCAPE_ALERT_TIERS
        tiers = ESCAPE_ALERT_TIERS
    except Exception:
        tiers = ()
    if tiers and top_score is not None:
        hit = next((n for f, n in tiers if top_score >= f), None)
        floors = "/".join(str(f) for f, _ in tiers)
        rows.append(f"1 逃頂警報    分數 {top_score}／門檻 {floors}"
                    + (f"  🔴 {hit}" if hit else "  ⚪ 未達"))
    else:
        rows.append("1 逃頂警報    —")

    # 2 行動翻轉
    lbl = st.get("last_action_label")
    rows.append("2 行動翻轉    " + (f"上次 {lbl}" if lbl else "尚無紀錄"))

    # 3 馬丁重啟
    mk = st.get("last_mart_restart_key")
    rows.append("3 馬丁重啟    " + (f"上次 {mk}" if mk else "尚無紀錄"))

    # 4 升槓桿窗口
    if gate and gate.get("ok") is not None:
        g1 = "✅" if gate.get("g1") else "✕"
        g2 = "✅" if gate.get("g2") else "✕"
        rows.append(f"4 升槓桿窗口  AHR999 {gate.get('ahr'):.3f}{g1}  距ATH {gate.get('dath')}天{g2}"
                    + ("  🟢 開窗中" if gate.get("ok") else "  ⚪ 未開"))
    else:
        rows.append("4 升槓桿窗口  —")

    # 5 熊底確認 D3（含 2026-08-25 新增的 c3「仍在熊市」閘門）
    if d3 and d3.get("ok") is not None:
        c1 = "✅" if d3.get("c1") else "✕"
        c2 = "✅" if d3.get("c2") else "✕"
        c3 = "" if d3.get("c3", True) else "  ⚠c3未過(距ATH太近)"
        rows.append(f"5 熊底確認D3  反彈 {d3.get('rebound', 0) * 100:+.1f}%{c1}"
                    f"  距低 {d3.get('days', 0)}天{c2}{c3}  {_mark(st.get('d3_confirmed'))}")
    else:
        rows.append("5 熊底確認D3  —  " + _mark(st.get("d3_confirmed")))

    # 6 套保建倉（G3 前提 + 三批）
    if rsi14 is not None and rsi_max_90d is not None:
        armed = rsi_max_90d > HEDGE_G3_PEAK
        done = [n for n, _, _ in HEDGE_BATCHES if st.get(f"hedge_batch_{n}")]
        nxt = next((f"<{thr}" for n, thr, _ in HEDGE_BATCHES
                    if n not in done and rsi14 >= thr), None)
        pre = "G3✅" if armed else f"G3✕(近90日峰 {rsi_max_90d:.0f}，需>{HEDGE_G3_PEAK})"
        rows.append(f"6 套保建倉    RSI {rsi14:.1f}  {pre}"
                    f"  已建 {len(done)}/3" + (f"  下一批 RSI {nxt}" if nxt and armed else ""))
    else:
        done = [n for n, _, _ in HEDGE_BATCHES if st.get(f"hedge_batch_{n}")]
        rows.append(f"6 套保建倉    —  已建 {len(done)}/3")

    # 7 週報
    wd = st.get("last_weekly_date")
    rows.append("7 週報        " + (f"上次 {wd}" if wd else "尚無紀錄"))
    return rows
