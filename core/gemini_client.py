"""
core/gemini_client.py
Gemini API 輕量封裝 — 供新聞中文化／摘要使用

設計決策（為何走 REST 而非官方 SDK）：
  1. 零新依賴：複用 core.http_client.safe_post（已含重試/逾時/偽裝 UA）
  2. SSL：自動套用 verify=SSL_VERIFY，本地公司網路 SSL 攔截直接繞過
     （官方 google-genai SDK 反而難以關閉憑證驗證）
  3. 金鑰走 x-goog-api-key header，不進 URL → 不會被 http_client 的錯誤日誌洩漏

金鑰來源（依序）：
  1. 環境變數 GOOGLE_API_KEY（本地 .env，雲端 Streamlit Cloud Secrets 也會注入為環境變數）
  2. st.secrets["GOOGLE_API_KEY"]（雲端保險）

無金鑰或呼叫失敗時一律回 None，讓呼叫端 fallback（例：新聞顯示英文原標題），不丟例外。
"""
import os
import logging
from typing import Optional

from dotenv import load_dotenv

from config import SSL_VERIFY
from core.http_client import safe_post

load_dotenv()
logger = logging.getLogger(__name__)

# gemini-2.5-flash：便宜、快、足夠翻譯/摘要任務
# （gemini-2.0-flash 雖列在 ListModels 但已下架，呼叫回 404 "no longer available"）
_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"
)


def _get_api_key() -> Optional[str]:
    """取得 GOOGLE_API_KEY：環境變數優先，其次 st.secrets。"""
    key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if key:
        return key
    try:
        import streamlit as st
        key = (st.secrets.get("GOOGLE_API_KEY", "") or "").strip()
        return key or None
    except Exception:
        return None


def is_available() -> bool:
    """是否有可用金鑰（供呼叫端決定要不要走 Gemini 路徑）。"""
    return _get_api_key() is not None


def generate(
    prompt: str,
    *,
    temperature: float = 0.3,
    max_output_tokens: int = 2048,
    thinking_budget: int = 0,
    timeout: int = 30,
) -> Optional[str]:
    """送出單一 prompt，回傳模型純文字輸出；任何失敗回 None。

    thinking_budget：gemini-2.5 系列為 reasoning 模型，預設會「思考」並吃掉
      output token，翻譯/摘要這類任務不需思考。預設 0 = 關閉思考，
      讓 maxOutputTokens 全留給實際輸出（又快又省 token）。
    """
    key = _get_api_key()
    if not key:
        return None

    headers = {
        "x-goog-api-key": key,
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingBudget": thinking_budget},
        },
    }

    try:
        r = safe_post(
            _GEMINI_ENDPOINT,
            headers=headers,
            json=body,
            timeout=timeout,
            verify=SSL_VERIFY,
        )
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # 可能被安全機制擋下或無輸出
            logger.warning("[gemini] no candidates in response: %s", data.get("promptFeedback"))
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as e:
        logger.warning("[gemini] generate failed: %s", e)
        return None
