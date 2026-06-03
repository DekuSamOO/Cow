"""
service/news_i18n.py
新聞中文化 — Gemini 批次翻譯標題＋產生中文小結＋判定情緒

省 token 策略（對應使用者需求「避免太消耗 Gemini token」）：
  1. 批次：一次 prompt 處理整批新聞（最多 _BATCH 則），回 JSON，避免逐則往返
  2. 持久化快取：翻譯結果以 url 為 key 存 db/news_i18n.json，翻過的永不重翻
     → Gemini 真正被呼叫的量 = 「本批新出現、且過去沒翻過的新聞則數」
     與 Streamlit Cloud 休眠/喚醒次數脫鉤（同一則新聞只翻一次）
  3. 上層 fetch_crypto_news 另有 @st.cache_data(ttl=14400) 4 小時記憶體快取

降級：無金鑰或 Gemini 失敗時，title_zh/summary_zh 留 None，UI 顯示英文原文。
"""
import os
import json
import logging
from typing import Any, Dict, List

from config import NEWS_I18N_ENABLED
from core import gemini_client

logger = logging.getLogger(__name__)

_BATCH = 8
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_PATH = os.path.join(_REPO_ROOT, "db", "news_i18n.json")
_CACHE_MAX = 300   # 持久化快取最多保留筆數（超過刪最舊）

_VALID_SENTIMENT = {"bull", "bear", "neutral"}


# ──────────────────────────────────────────────────────────────────────────
# 持久化快取
# ──────────────────────────────────────────────────────────────────────────
def _load_cache() -> Dict[str, dict]:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: Dict[str, dict]) -> None:
    # 超量時保留最後 _CACHE_MAX 筆（dict 在 py3.7+ 保留插入序）
    if len(cache) > _CACHE_MAX:
        cache = dict(list(cache.items())[-_CACHE_MAX:])
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[news_i18n] save cache failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────
# Gemini 批次翻譯
# ──────────────────────────────────────────────────────────────────────────
def _build_prompt(batch: List[Any]) -> str:
    payload = [
        {
            "id": i,
            "title": it.title,
            "summary": (it.raw_summary or "")[:300],
        }
        for i, it in enumerate(batch)
    ]
    return (
        "你是加密貨幣財經編輯。以下是一批英文新聞（JSON 陣列，每筆含 id/title/summary）。\n"
        "請為每一筆輸出：\n"
        "  title_zh：繁體中文標題翻譯（精簡、通順、保留幣種與專有名詞）\n"
        "  summary_zh：1-2 句繁體中文重點摘要（若 summary 為空則依 title 推測重點）\n"
        "  sentiment：對比特幣/加密市場的情緒傾向，只能是 bull、bear、neutral 三者之一\n"
        "嚴格只輸出 JSON 陣列，每個元素為 {id, title_zh, summary_zh, sentiment}，"
        "不要任何說明文字或 markdown 標記。\n\n"
        f"輸入：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_json_array(text: str) -> List[dict]:
    """從 Gemini 輸出抽出 JSON 陣列（容忍 ```json 包裹或前後雜訊）。"""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _translate_batch(batch: List[Any]) -> None:
    """呼叫 Gemini 翻譯一批，原地填入 title_zh/summary_zh/sentiment。失敗則保持 None。"""
    out = gemini_client.generate(_build_prompt(batch), max_output_tokens=2048)
    if not out:
        return
    for rec in _parse_json_array(out):
        try:
            idx = int(rec.get("id"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(batch)):
            continue
        it = batch[idx]
        it.title_zh = (rec.get("title_zh") or "").strip() or None
        it.summary_zh = (rec.get("summary_zh") or "").strip() or None
        sent = (rec.get("sentiment") or "").strip().lower()
        it.sentiment = sent if sent in _VALID_SENTIMENT else "neutral"


# ──────────────────────────────────────────────────────────────────────────
# 對外入口
# ──────────────────────────────────────────────────────────────────────────
def enrich_news_zh(items: List[Any]) -> None:
    """原地為 items 填入中文標題/小結/情緒。停用/無金鑰則直接 return（UI fallback 英文）。"""
    if not NEWS_I18N_ENABLED or not items or not gemini_client.is_available():
        return

    cache = _load_cache()

    # 1) 先用持久化快取回填
    todo: List[Any] = []
    for it in items:
        hit = cache.get(it.url)
        if hit:
            it.title_zh = hit.get("title_zh")
            it.summary_zh = hit.get("summary_zh")
            it.sentiment = hit.get("sentiment")
        else:
            todo.append(it)

    if not todo:
        return

    # 2) 未命中者分批丟 Gemini
    dirty = False
    for i in range(0, len(todo), _BATCH):
        batch = todo[i:i + _BATCH]
        _translate_batch(batch)
        for it in batch:
            if it.title_zh:   # 成功翻譯才寫快取
                cache[it.url] = {
                    "title_zh": it.title_zh,
                    "summary_zh": it.summary_zh,
                    "sentiment": it.sentiment,
                }
                dirty = True

    if dirty:
        _save_cache(cache)
