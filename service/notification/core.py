import os
import json
import requests
from core.http_client import safe_get, safe_post
import urllib3
from datetime import datetime
from dotenv import load_dotenv

# 從集中設定檔讀取 SSL 旗標
from config import SSL_VERIFY

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# ── LINE Bot 憑證 ────────────────────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID              = os.getenv("LINE_USER_ID", "")
_LINE_PUSH_URL            = "https://api.line.me/v2/bot/message/push"

# ── Telegram Bot 憑證 ────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
_TELEGRAM_API_URL  = "https://api.telegram.org/bot{token}/sendMessage"

def _is_line_configured() -> bool:
    return bool(LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID)

def _is_telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def _send_line_message(messages: list[dict]) -> bool:
    if not _is_line_configured():
        print("[LINE Notifier] 未設定，跳過（請在 .env 設定 LINE_CHANNEL_ACCESS_TOKEN）")
        return False

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to":       LINE_USER_ID,
        "messages": messages,
    }

    try:
        resp = safe_post(
            _LINE_PUSH_URL,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False),
            timeout=8,
            verify=SSL_VERIFY,
        )
        if resp.status_code == 200:
            print(f"[LINE Notifier] 推播成功: HTTP {resp.status_code}")
            return True
        else:
            print(f"[LINE Notifier] 推播失敗: HTTP {resp.status_code} - {resp.text[:200]}")
            return False
    except requests.exceptions.Timeout:
        print("[LINE Notifier] 推播逾時")
        return False
    except Exception as e:
        print(f"[LINE Notifier] 推播例外: {e}")
        return False

def _send_telegram_message(text: str, parse_mode: str = "HTML") -> bool:
    if not _is_telegram_configured():
        print("[Telegram Notifier] 未設定，跳過（請在 .env 設定 TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID）")
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
            print(f"[Telegram Notifier] 推播成功: HTTP {resp.status_code}")
            return True
        else:
            err_desc = resp.json().get('description', resp.text[:200])
            print(f"[Telegram Notifier] 推播失敗: HTTP {resp.status_code} - {err_desc}")
            return False
    except requests.exceptions.Timeout:
        print("[Telegram Notifier] 推播逾時")
        return False
    except Exception as e:
        print(f"[Telegram Notifier] 推播例外: {e}")
        return False
