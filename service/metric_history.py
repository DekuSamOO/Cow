"""
service/metric_history.py · 指標歷史序列（供 PiT 滾動分位使用）

2026-08-25 建立。core 的抄底子項（RSI／SOPR／F&G）改為「絕對階梯 ∪ PiT 滾動分位」後，
評分當下需要一段歷史。core 維持純函數零網路 → 取數與快取一律由本模組負責。

  RSI   不在這裡：它已經在呼叫端傳入的日線 df 裡（core 直接取用，不需管線）
  SOPR  ← db/bottom_metrics_cache.json 的 sopr（2022-08+ 逐日）
  F&G   ← db/cache/fng_history.json（alternative.me 全史，2018-02+）

離線行為：讀不到就回空 list → core 端自動退回純絕對階梯，不讓面板掛掉。
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOPR_CACHE = os.path.join(_COW, "db", "bottom_metrics_cache.json")
FNG_CACHE = os.path.join(_COW, "db", "cache", "fng_history.json")
FNG_API = "https://api.alternative.me/fng/?limit=0&format=json"
_MIN_REFRESH_SEC = 6 * 3600

_mem = {"fng_ts": 0.0}


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("讀取 %s 失敗（%s）", path, e)
        return {}


def _tail_by_date(mapping: dict, upto: str = None, window: int = 400) -> list:
    """
    {日期字串: 值} → 依日期排序後取「<= upto 的最後 window 筆」。
    upto=None 取到最新。**呼叫端要負責 upto 不含未來**（回測時務必傳當日）。
    """
    if not mapping:
        return []
    keys = sorted(k for k in mapping if not str(k).startswith("_"))
    if upto:
        keys = [k for k in keys if k <= upto]
    out = []
    for k in keys[-window:]:
        try:
            out.append(float(mapping[k]))
        except (TypeError, ValueError):
            continue
    return out


def sopr_hist(upto: str = None, window: int = 400) -> list:
    """近 window 日 SOPR（含今日、不含未來）。取不到回 []。"""
    return _tail_by_date((_load_json(SOPR_CACHE) or {}).get("sopr") or {}, upto, window)


def refresh_fng_cache() -> dict:
    """向 alternative.me 增量更新 F&G 全史快取（best-effort，失敗不拋）。"""
    now = time.time()
    if (now - _mem["fng_ts"]) < _MIN_REFRESH_SEC and os.path.exists(FNG_CACHE):
        return _load_json(FNG_CACHE)
    try:
        import datetime
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        s = requests.Session()
        s.verify = False        # 公司網路為 SSL 攔截（見 _governance/ENV-notes.md）
        data = s.get(FNG_API, timeout=30).json().get("data") or []
        out = {}
        for x in data:
            d = datetime.date.fromtimestamp(int(x["timestamp"])).isoformat()
            out[d] = float(x["value"])
        if out:
            os.makedirs(os.path.dirname(FNG_CACHE), exist_ok=True)
            with open(FNG_CACHE, "w", encoding="utf-8") as f:
                json.dump(out, f)
            _mem["fng_ts"] = now
            return out
    except Exception as e:
        logger.warning("F&G 歷史更新失敗（%s）→ 用既有快取", e)
    return _load_json(FNG_CACHE)


def fng_hist(upto: str = None, window: int = 400, refresh: bool = True) -> list:
    """近 window 日 恐懼貪婪指數（含今日、不含未來）。取不到回 []。"""
    data = refresh_fng_cache() if refresh else _load_json(FNG_CACHE)
    return _tail_by_date(data or {}, upto, window)
