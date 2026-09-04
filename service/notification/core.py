import os
import json
import logging
import requests
from core.http_client import safe_get, safe_post
import urllib3
from datetime import datetime
from dotenv import load_dotenv

# 從集中設定檔讀取 SSL 旗標
from config import SSL_VERIFY

logger = logging.getLogger(__name__)

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# ── LINE Bot 憑證 ────────────────────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID              = os.getenv("LINE_USER_ID", "")
_LINE_PUSH_URL            = "https://api.line.me/v2/bot/message/push"

# ── U5-①：防守專用 LINE 通道（2026-07-14）─────────────────────────────────
# 未設定時 fallback 至上方日常憑證（現況：同一支 bot，靠重複策略區隔）；
# 日後建立防守專用 channel 只需填這兩個 env/secret，零代碼改動即分流。
DEFENSE_LINE_CHANNEL_ACCESS_TOKEN = os.getenv("DEFENSE_LINE_CHANNEL_ACCESS_TOKEN", "")
DEFENSE_LINE_USER_ID              = os.getenv("DEFENSE_LINE_USER_ID", "")

# ── Telegram Bot 憑證 ────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
_TELEGRAM_API_URL  = "https://api.telegram.org/bot{token}/sendMessage"

def _is_line_configured() -> bool:
    return bool(LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID)

def _is_telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def _outbound_allowed(label: str, preview: str = "") -> bool:
    """對外推播的單一閘門（2026-09-04 立）。**所有推播路徑都必須先過這裡。**

    ── 為什麼要有這道閘門 ────────────────────────────────────────────────
    2026-09-02：一個 subagent 在本機直接跑 `python scripts/daily_line_notify.py`，
    **真的把當日完整 Flex 卡片推到使用者手機**——它以為那只是演練。
    成因是 `__main__` 沒有 dry_run 參數、憑證由 `.env` 的 `load_dotenv()` 自動載入，
    所以「在本機試一下」在這支腳本裡等於真的送出去。

    全域規則 §0.4「對外發送預設 dry-run，真實推播前等核准」擋不住這種事：
    **規則是建議性的，閘門才是確定性的。** 所以改成本機預設不送。

    ── 判定順序（先到者為準）─────────────────────────────────────────────
      1. `DRY_RUN` 有明確設值 → 照它走
         （"0"/"false"/"no"/"off" = 允許真送；其餘任何非空值 = 擋下）
      2. 沒設 `DRY_RUN` → **只有在 GitHub Actions 內（`GITHUB_ACTIONS` 有值）才允許真送**
      3. 其餘（本機、subagent、任何非 CI 環境）一律擋下，並印出本來要送的內容摘要

    要在本機真的送一則（例如驗證憑證），必須**明確**寫 `DRY_RUN=0`，
    這一步的顯式性就是「核准」本身。
    """
    raw = os.getenv("DRY_RUN")
    if raw is not None and raw.strip() != "":
        allowed = raw.strip().lower() in ("0", "false", "no", "off")
        why = f"DRY_RUN={raw.strip()}"
    else:
        allowed = bool(os.getenv("GITHUB_ACTIONS"))
        why = "GITHUB_ACTIONS 有值" if allowed else "非 CI 環境且未設 DRY_RUN=0"
    if not allowed:
        msg = f"[{label}] 🚫 對外推播已被閘門擋下（{why}）。本機要真送請明確設 DRY_RUN=0。"
        logger.warning(msg)
        print(msg)
        if preview:
            print(f"[{label}] 本來要送的內容：{preview[:300]}")
    return allowed


def _preview_of(messages: list[dict]) -> str:
    """把 messages 壓成一行摘要，供閘門擋下時顯示（不做完整序列化，避免洗版）。"""
    out = []
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        if m.get("type") == "text":
            out.append(str(m.get("text", ""))[:160].replace("\n", " / "))
        else:
            out.append(f"<{m.get('type', '?')}>")
    return " | ".join(out)


def _push_line(token: str, user_id: str, messages: list[dict], label: str = "LINE Notifier") -> bool:
    """LINE push API 共用發送（日常/防守通道共用；憑證由呼叫端決定）。

    ⚠️ 本函式是**所有** LINE 推播的咽喉點（日常 `_send_line_message` 與防守
    `_send_defense_line_message` 都走這裡），閘門放這裡才擋得住每一條路徑。
    """
    if not _outbound_allowed(label, _preview_of(messages)):
        return False
    headers = {
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "to":       user_id,
        "messages": messages,
    }

    try:
        resp = safe_post(
            _LINE_PUSH_URL,
            headers=headers,
            json=payload,
            timeout=8,
            verify=SSL_VERIFY,
        )
        if resp.status_code == 200:
            logger.info(f"[{label}] 推播成功: HTTP {resp.status_code}")
            return True
        else:
            logger.warning(f"[{label}] 推播失敗: HTTP {resp.status_code} - {resp.text[:200]}")
            return False
    except requests.exceptions.Timeout:
        logger.warning(f"[{label}] 推播逾時")
        return False
    except Exception as e:
        logger.info(f"[{label}] 推播例外: {e}")
        return False


def _send_line_message(messages: list[dict]) -> bool:
    if not _is_line_configured():
        logger.warning("[LINE Notifier] 未設定，跳過（請在 .env 設定 LINE_CHANNEL_ACCESS_TOKEN）")
        return False
    return _push_line(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, messages)


def _send_defense_line_message(messages: list[dict]) -> bool:
    """U5-①：防守警報專用發送——優先走防守通道，未設定 fallback 日常通道。

    fallback 是刻意設計（fail-safe）：防守警報寧可混進日常通道也不可丟失。
    """
    if DEFENSE_LINE_CHANNEL_ACCESS_TOKEN and DEFENSE_LINE_USER_ID:
        return _push_line(DEFENSE_LINE_CHANNEL_ACCESS_TOKEN, DEFENSE_LINE_USER_ID,
                          messages, label="Defense LINE")
    return _send_line_message(messages)

def _send_telegram_message(text: str, parse_mode: str = "HTML") -> bool:
    if not _is_telegram_configured():
        logger.warning("[Telegram Notifier] 未設定，跳過（請在 .env 設定 TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID）")
        return False
    # Telegram 也是對外發送，同一道閘門（2026-09-04）——只擋 LINE 會留下另一條路
    if not _outbound_allowed("Telegram Notifier", str(text)[:160]):
        return False

    url = _TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = safe_post(
            url,
            json=payload,
            timeout=8,
            verify=SSL_VERIFY,
        )
        if resp.status_code == 200:
            logger.info(f"[Telegram Notifier] 推播成功: HTTP {resp.status_code}")
            return True
        else:
            err_desc = resp.json().get('description', resp.text[:200])
            logger.warning(f"[Telegram Notifier] 推播失敗: HTTP {resp.status_code} - {err_desc}")
            return False
    except requests.exceptions.Timeout:
        logger.warning("[Telegram Notifier] 推播逾時")
        return False
    except Exception as e:
        logger.info(f"[Telegram Notifier] 推播例外: {e}")
        return False
