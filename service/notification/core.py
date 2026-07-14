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

def _push_line(token: str, user_id: str, messages: list[dict], label: str = "LINE Notifier") -> bool:
    """LINE push API 共用發送（日常/防守通道共用；憑證由呼叫端決定）。"""
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
