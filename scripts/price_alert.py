"""
scripts/price_alert.py
1 BTC ROAD 價格警報腳本（GitHub Actions 每小時觸發）

監控關鍵門檻：
  - BTC <= config.ALERT_PRICE_LOW（防守線）→ 防守事件：馬丁格爾轉換 + 補保證金

防重複推播：使用 alert_state.json 記錄今日已推播的警報，
每個警報每個曆日最多推一次，避免 BTC 在門檻附近震盪時狂轟。
"""

import json
import os
import sys
from core.http_client import safe_get
import urllib3
from datetime import datetime, timezone


from config import (SSL_VERIFY, ALERT_PRICE_LOW, ALERT_PRICE_REARM_GAP,
                    DEFENSE_DECISION_WINDOW_H, DEFENSE_REMINDER_HOURS)
from service.notification.facade import (notify_defense_line, notify_defense_reminder,
                                         notify_defense_window_close)

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = os.path.dirname(_SCRIPT_DIR)

# GitHub Actions artifact 下載後放在 repo 根目錄
STATE_FILE = os.path.join(_REPO_ROOT, "alert_state.json")


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_defense_date": None}  # armed_defense 缺鍵時由呼叫端 .get(..., True) 視為已武裝


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _now() -> datetime:
    """UTC now（抽函式供測試凍結時間；main 內日期/窗計時一律同源自此）。"""
    return datetime.now(timezone.utc)


def _should_alert(last_date: str | None) -> bool:
    """當日曆日與上次推播日期不同時才推播（每天最多一次）。"""
    return last_date != _now().date().isoformat()


def _due_reminder_idx(elapsed_h: float, sent_idx: int) -> int:
    """
    U5-①：回傳「已到期的最後一個里程碑 index＋1」（純函數供測試）。

    milestones = DEFENSE_REMINDER_HOURS（事件起算小時）。Actions 若停擺數小時，
    補推時只推最新一則、跳過中間積欠的（catch-up 不轟炸）。
    回傳值 > sent_idx 表示有新提醒要推；推播後把 state 的 idx 更新為回傳值。
    """
    idx = sent_idx
    for i in range(sent_idx, len(DEFENSE_REMINDER_HOURS)):
        if elapsed_h >= DEFENSE_REMINDER_HOURS[i]:
            idx = i + 1
    return idx


def fetch_btc_price() -> float | None:
    """透過 Coinbase 公開 API 取得 BTC 現價（GitHub Actions 環境適用）。"""
    try:
        resp = safe_get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=10,
            verify=SSL_VERIFY,
        )
        resp.raise_for_status()
        price = float(resp.json()["data"]["amount"])
        print(f"✅ BTC 現價: ${price:,.0f}")
        return price
    except Exception as e:
        print(f"❌ 取得 BTC 現價失敗: {e}")
        return None


def main() -> None:
    price = fetch_btc_price()
    if price is None:
        print("無法取得現價，本次跳過警報檢查。")
        sys.exit(0)

    state = _load_state()
    today = _now().date().isoformat()
    armed = state.get("armed_defense", True)  # 舊 state 檔無此鍵 → 視為已武裝

    # ── 防守事件：防守線警報（遲滯：單次跌破只推一次全量，回升超過門檻+GAP 才重新武裝）──
    # U5-①（2026-07-14）：全量警報後進入 24h 決策窗——窗內按 DEFENSE_REMINDER_HOURS
    # 里程碑重推短提醒，屆滿推「窗關閉＝預設不防守（U5-②）」後本事件靜默直到 rearm。
    rearm_price = ALERT_PRICE_LOW + ALERT_PRICE_REARM_GAP
    if price <= ALERT_PRICE_LOW:
        if armed and _should_alert(state.get("last_defense_date")):
            print(f"🛡️  防守事件：BTC ${price:,.0f} <= ${ALERT_PRICE_LOW:,.0f}，發送防守警報（三連響）")
            notify_defense_line(price)
            state["last_defense_date"] = today
            state["armed_defense"] = False  # 解除武裝：全量警報只推一次，後續走決策窗提醒
            state["defense_event_start"] = _now().isoformat(timespec="seconds")
            state["defense_reminder_idx"] = 0
            state["defense_window_closed"] = False
        elif not armed and state.get("defense_event_start") and not state.get("defense_window_closed"):
            elapsed_h = (_now() - datetime.fromisoformat(state["defense_event_start"])
                         ).total_seconds() / 3600.0
            if elapsed_h >= DEFENSE_DECISION_WINDOW_H:
                print(f"🔒 決策窗屆滿（{elapsed_h:.1f}h ≥ {DEFENSE_DECISION_WINDOW_H}h），推播窗關閉（預設不防守）")
                notify_defense_window_close(price)
                state["defense_window_closed"] = True
            else:
                sent_idx = state.get("defense_reminder_idx", 0)
                due_idx = _due_reminder_idx(elapsed_h, sent_idx)
                if due_idx > sent_idx:
                    print(f"⏰ 決策窗提醒：事件後 {elapsed_h:.1f}h，推播第 {due_idx} 則里程碑提醒")
                    notify_defense_reminder(price, elapsed_h, due_idx)
                    state["defense_reminder_idx"] = due_idx
                else:
                    print(f"ℹ️  決策窗內（{elapsed_h:.1f}h），下個里程碑未到，略過。")
        else:
            reason = ("今日已推播" if armed
                      else f"事件已收尾（回升至 ${rearm_price:,.0f} 才重新武裝）")
            print(f"ℹ️  BTC ${price:,.0f} <= ${ALERT_PRICE_LOW:,.0f}，{reason}，略過。")
    else:
        if not armed and price >= rearm_price:
            state["armed_defense"] = True
            state.pop("defense_event_start", None)   # U5-①：清事件狀態，下次跌破重新開窗
            state.pop("defense_reminder_idx", None)
            state.pop("defense_window_closed", None)
            print(f"🔄 BTC ${price:,.0f} >= ${rearm_price:,.0f}，防守警報重新武裝。")
        else:
            print(f"✓ BTC ${price:,.0f} > ${ALERT_PRICE_LOW:,.0f}，未觸及防守門檻。")

    _save_state(state)


if __name__ == "__main__":
    main()
