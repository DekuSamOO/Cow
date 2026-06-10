"""
scripts/price_alert.py
1 BTC ROAD 價格警報腳本（GitHub Actions 每小時觸發）

監控關鍵門檻：
  - BTC <= $58,000 → 觸發事件二：馬丁格爾轉換 + 補保證金

防重複推播：使用 alert_state.json 記錄今日已推播的警報，
每個警報每個曆日最多推一次，避免 BTC 在門檻附近震盪時狂轟。
"""

import json
import os
import sys
import requests
from core.http_client import safe_get, safe_post
import urllib3
from datetime import date


from config import SSL_VERIFY, ALERT_PRICE_LOW, ALERT_PRICE_REARM_GAP
from service.notification.facade import notify_58k_defense

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
    return {"last_58k_date": None}  # armed_58k 缺鍵時由呼叫端 .get(..., True) 視為已武裝


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _should_alert(last_date: str | None) -> bool:
    """當日曆日與上次推播日期不同時才推播（每天最多一次）。"""
    return last_date != str(date.today())


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
    today = str(date.today())
    armed = state.get("armed_58k", True)  # 舊 state 檔無此鍵 → 視為已武裝

    # ── 觸發事件二：$58,000 防守警報（遲滯：單次跌破只推一次，回升超過門檻+GAP 才重新武裝）──
    rearm_price = ALERT_PRICE_LOW + ALERT_PRICE_REARM_GAP
    if price <= ALERT_PRICE_LOW:
        if armed and _should_alert(state.get("last_58k_date")):
            print(f"🛡️  觸發事件二：BTC ${price:,.0f} <= ${ALERT_PRICE_LOW:,.0f}，發送防守警報")
            notify_58k_defense(price)
            state["last_58k_date"] = today
            state["armed_58k"] = False  # 解除武裝：持續低於門檻不再重複推播
        else:
            reason = "今日已推播" if armed else f"已解除武裝（回升至 ${rearm_price:,.0f} 才重新武裝）"
            print(f"ℹ️  BTC ${price:,.0f} <= ${ALERT_PRICE_LOW:,.0f}，{reason}，略過。")
    else:
        if not armed and price >= rearm_price:
            state["armed_58k"] = True
            print(f"🔄 BTC ${price:,.0f} >= ${rearm_price:,.0f}，防守警報重新武裝。")
        else:
            print(f"✓ BTC ${price:,.0f} > ${ALERT_PRICE_LOW:,.0f}，未觸及防守門檻。")

    _save_state(state)


if __name__ == "__main__":
    main()
