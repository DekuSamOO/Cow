"""
core/watch_alerts.py · 警戒引擎（E2）— 觸價/訊號變化事件，純函數核心

波段執行可靠度第二槓桿：**不漏訊號**——進場區/停損/目標觸價與 composite 訊號變化
主動響鈴，不靠人盯盤（設計文件：Obsidian\\Github\\Cow\\20260704plan_watcher波段執行.md §E2）。

遲滯防抖 pattern 沿用 `scripts/price_alert.py`（2026-06 已實戰）：每個警戒各自「武裝」，
觸發即解除武裝，價格離開觸發區超過 rearm gap（預設 0.5%）才重新武裝——
免費源延遲價在門檻附近震盪時不狂響。

通知本地 only（winsound / 終端響鈴）；LINE 等對外推播明確不在 v1（CONSTITUTION：
對外發送預設 dry-run，要做另案走 notification facade）。
"""
import datetime
import json
import os
from typing import Dict, List, Optional, Tuple

from core.watch_plan import TradePlan

DEFAULT_REARM_PCT = 0.005   # 重新武裝需離開觸發價位 0.5%（免費源 20 分延遲下的合理噪音帶）

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_PATH = os.path.join(_REPO_ROOT, "logs", "watch_journal.jsonl")   # 已入 .gitignore


def _mk(symbol: str, kind: str, msg: str, price: Optional[float] = None) -> dict:
    return {"ts": datetime.datetime.now().strftime("%H:%M"),
            "symbol": symbol, "event": kind, "msg": msg, "price": price}


def check_price_events(plan: TradePlan, price: Optional[float], state: Optional[dict] = None,
                       rearm_pct: float = DEFAULT_REARM_PCT,
                       today: Optional[datetime.date] = None) -> Tuple[List[dict], dict]:
    """
    依交易計畫檢查觸價事件 → (events, new_state)。純函數：state 不就地修改。

    state 為該 symbol 的武裝旗標 dict（session 記憶體即可）：
      armed_entry / armed_stop / armed_target_{i}，缺鍵視為已武裝（同 price_alert.py 慣例）。
    過期計畫不觸發（過期照響＝雜訊，面板已另有過期標註）。
    """
    state = dict(state or {})
    if price is None or price <= 0 or plan.expired(today):
        return [], state
    events: List[dict] = []
    is_long = plan.direction == "long"
    gap = rearm_pct

    # 進場區：入區觸發；出區超過 gap（上下皆算）才重新武裝
    in_zone = plan.entry_low <= price <= plan.entry_high
    if in_zone and state.get("armed_entry", True):
        events.append(_mk(plan.symbol, "entry",
                          f"▶ 進入進場區（現價 {price:,.2f}｜區 {plan.entry_low:,.2f}~{plan.entry_high:,.2f}）", price))
        state["armed_entry"] = False
    elif not in_zone and not state.get("armed_entry", True):
        if price < plan.entry_low * (1 - gap) or price > plan.entry_high * (1 + gap):
            state["armed_entry"] = True

    # 停損：多單跌破 / 空單漲破
    hit_stop = price <= plan.stop if is_long else price >= plan.stop
    if hit_stop and state.get("armed_stop", True):
        events.append(_mk(plan.symbol, "stop",
                          f"🛑 觸及停損（現價 {price:,.2f}｜停損 {plan.stop:,.2f}）——依計畫執行，勿凹單", price))
        state["armed_stop"] = False
    elif not hit_stop and not state.get("armed_stop", True):
        rearmed = price >= plan.stop * (1 + gap) if is_long else price <= plan.stop * (1 - gap)
        if rearmed:
            state["armed_stop"] = True

    # 目標：各目標獨立武裝（T1 達成後 T2 警戒仍有效）
    for i, t in enumerate(plan.targets):
        key = f"armed_target_{i}"
        hit = price >= t if is_long else price <= t
        if hit and state.get(key, True):
            events.append(_mk(plan.symbol, f"target_{i + 1}",
                              f"🎯 達目標 T{i + 1}（現價 {price:,.2f}｜目標 {t:,.2f}）——分批止盈", price))
            state[key] = False
        elif not hit and not state.get(key, True):
            rearmed = price <= t * (1 - gap) if is_long else price >= t * (1 + gap)
            if rearmed:
                state[key] = True
    return events, state


def check_signal_change(symbol: str, action_key: Optional[str], action_label: Optional[str],
                        state: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """composite 行動建議變化事件（如 順勢持有→分批止盈）。同 key 去重；
    首次觀測不觸發（剛開儀表板的第一眼不是「變化」）。state 與觸價共用同一 per-symbol dict。"""
    state = dict(state or {})
    events: List[dict] = []
    prev = state.get("last_action")
    if prev is not None and action_key is not None and action_key != prev[0]:
        events.append(_mk(symbol, "signal", f"⚡ 訊號變化：{prev[1]} → {action_label}"))
    if action_key is not None:
        state["last_action"] = (action_key, action_label)
    return events, state


def banner_rows(events: List[dict]) -> List[str]:
    """事件 → 警戒橫幅顯示列（與 watcher 面板同縮排慣例）。"""
    return [f"  {e['ts']}  {e['symbol']}  {e['msg']}" for e in events]


def journal_append(record: dict, path: Optional[str] = None) -> None:
    """觸發/執行事件寫 logs/watch_journal.jsonl（一行一筆，E3）。
    「計畫→觸發→執行」三段留痕，週末對帳計畫 vs 執行偏差用。
    寫入失敗靜默不中斷監控（日誌是輔助，不能反過來干擾盯盤）。"""
    path = path or JOURNAL_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def journal_record(event: dict, snapshot: Optional[dict] = None) -> dict:
    """警戒事件＋觸發當下訊號快照 → 日誌記錄（完整 ISO 時間戳；快照供事後對「當時訊號長怎樣」）。"""
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "symbol": event["symbol"], "event": event["event"],
           "price": event.get("price"), "msg": event["msg"]}
    rec.update(snapshot or {})
    return rec


def notify_beep() -> None:
    """本地響鈴：Windows 用 winsound（兩短音），其餘退終端 bell。失敗靜默（響鈴是輔助）。"""
    try:
        import winsound
        for _ in range(2):
            winsound.Beep(880, 250)
    except Exception:  # noqa: BLE001
        print("\a", end="")
