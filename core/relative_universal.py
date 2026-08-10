"""
core/relative_universal.py
通用逃頂/抄底子訊號 — 純函數、零網路請求，不依賴任何市場專屬籌碼資料
（不需融資融券／三大法人／TDCC／PE-PB），只吃標準 OHLCV（open/high/low/close/volume），
因此台股（`relative_high_tw`/`relative_low_tw`）、美股（未來新框架）、加密都能共用。

含兩組訊號：
  量價背離（2a/2b）：量增價縮＝出貨、量縮價增＝賣壓竭盡（惜售）。
  結構轉折（2c/2d）：複用 `core.divergence.detect_swing_structure` 判斷波段高低點結構，
    前高未過／前低未破視為頭部／底部轉折警訊。

⚠️ 狀態：**目前所有訊號皆為規則式（rule-based），尚未經 swing 回測校準**——比照台股
`core/relative_high_tw.py`/`relative_low_tw.py` 的 `WEAK_DIMS_*_TW` 方法論：先以低權重/
參考項上線，待累積足夠跨市場資料後再回測校準（見 tests/core/*_backtest.py 系列的既有作法），
分級門檻與配分皆為專家經驗值，非統計擬合結果。呼叫端若把這裡的分數併入既有逃頂/抄底
框架（如台股 100 分制），須連同既有維度一起重新按比例分配權重（本模組本身的 max
15/15/10/10 只是暫定值）。
"""
import math
from typing import Dict, Optional, Tuple

import pandas as pd

from core.divergence import detect_swing_structure


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def rescale_dim(sig: dict, new_max: int) -> dict:
    """按比例縮放子維分數到新配額（score/max 按比例換算，label/note/sub 不變）。

    供呼叫端把本模組的訊號疊加進既有已校準框架時使用（例如台股逃頂/抄底六維已用真實
    swing 回測配重，本模組的量價/結構訊號尚無回測數據，不應用臆測數字覆蓋既有配重，
    改用較小配額疊加——這裡就是做這個換算，不動 sig 本身的判讀邏輯）。"""
    old_max = sig.get("max", 0)
    if old_max <= 0:
        return {**sig, "max": new_max}
    return {**sig, "score": round(sig["score"] * new_max / old_max), "max": new_max}


def high_meta_ladder(score: int, top_action: str) -> Tuple[str, str, str]:
    """逃頂 5 級門檻階梯（65/45/30/15，含 label/顏色）共用。台股/美股 `relative_high_*_meta`
    的門檻與顏色本就刻意一致（僅最高分那句操作建議因各框架最強驅動維度不同而客製），
    抽出避免逐字複製；未來加密以外的新市場沿用同一套門檻時可直接複用。"""
    if score >= 65: return "🔴 強烈逃頂", "#ff4b4b", top_action
    if score >= 45: return "🟠 明確過熱", "#ff8800", "減碼、收緊移動止盈"
    if score >= 30: return "🟡 偏熱警戒", "#ffcc00", "停止加倉、提高警覺"
    if score >= 15: return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🟢 無過熱", "#00cc88", "無逃頂壓力"


def low_meta_ladder(score: int, top_action: str) -> Tuple[str, str, str]:
    """抄底 5 級門檻階梯（65/45/30/15，含 label/顏色）共用，理由同 `high_meta_ladder`。"""
    if score >= 65: return "🟢 強力低估", "#00cc88", top_action
    if score >= 45: return "🟢 明確低估", "#00aa66", "可開始定投/減空"
    if score >= 30: return "🟡 偏冷觀察", "#ffcc00", "留意打底，勿純憑超賣搶反彈"
    if score >= 15: return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🔴 無底部訊號", "#ff4b4b", "無低估壓力，勿接刀"


def avg_vol(df, window: int = 20) -> Optional[float]:
    """近 `window` 日均量（<5 根或均量非正回 None）。台股逃頂/抄底的「法人買賣超/均量」
    正規化分母共用此式——原本 high/low 各存一份逐字相同的拷貝，改一邊漏一邊會讓同一次
    render 的兩張面板用不同分母，故收斂到這裡當單一來源。"""
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < 5:
        return None
    v = float(df["volume"].tail(window).mean())
    return v if v > 0 else None


def _vol_ratio(df: pd.DataFrame, short: int, long: int) -> Optional[float]:
    """近 short 日均量 / 近 long 日均量。資料不足或均量為 0 回 None。"""
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < long:
        return None
    vol = df["volume"]
    v_short = float(vol.tail(short).mean())
    v_long = float(vol.tail(long).mean())
    if v_long <= 0 or _nan(v_short) or _nan(v_long):
        return None
    return v_short / v_long


def _price_ret_chg(df: pd.DataFrame, short: int) -> Optional[Dict[str, float]]:
    """近 short 日報酬率（ret_now）與前一段同長度報酬率（ret_prior）。
    需要 close 欄且 len(df) >= 2*short+1。"""
    if df is None or "close" not in getattr(df, "columns", []) or len(df) < 2 * short + 1:
        return None
    close = df["close"]
    c0 = float(close.iloc[-1])
    c1 = float(close.iloc[-1 - short])
    c2 = float(close.iloc[-1 - 2 * short])
    if _nan(c0) or _nan(c1) or _nan(c2) or c1 == 0 or c2 == 0:
        return None
    ret_now = c0 / c1 - 1
    ret_prior = c1 / c2 - 1
    return {"ret_now": ret_now, "ret_prior": ret_prior}


def score_volume_price_top(df: pd.DataFrame, vol_short: int = 5, vol_long: int = 20,
                            price_short: int = 5) -> dict:
    """量價背離（逃頂用，max 15）：量增價縮＝買盤湧入卻推不動價格，出貨/籌碼鬆動訊號。"""
    max_score = 15
    if df is None or len(df) < max(vol_long, 2 * price_short + 1):
        return {"score": 0, "max": max_score, "label": "量價 ⚪ 資料不足",
                "note": "量增價縮＝出貨訊號（規則式，未回測校準，僅供參考）", "sub": {}}
    vol_ratio = _vol_ratio(df, vol_short, vol_long)
    ret = _price_ret_chg(df, price_short)
    if vol_ratio is None or ret is None:
        return {"score": 0, "max": max_score, "label": "量價 ⚪ 資料不足",
                "note": "量增價縮＝出貨訊號（規則式，未回測校準，僅供參考）", "sub": {}}
    ret_now, ret_prior = ret["ret_now"], ret["ret_prior"]

    if vol_ratio >= 1.5 and ret_now < ret_prior and ret_now < 0:
        score, label = 15, f"量價 🔴 量增{vol_ratio:.1f}x＋價轉弱（出貨徵兆）"
    elif vol_ratio >= 1.3 and ret_now < ret_prior:
        score, label = 8, f"量價 🟠 量增{vol_ratio:.1f}x＋動能收斂"
    else:
        score, label = 0, f"量價 ⚪ 量{vol_ratio:.1f}x（無背離）"

    return {"score": score, "max": max_score, "label": label,
            "note": "量增價縮＝出貨訊號（規則式，未回測校準，僅供參考）",
            "sub": {"vol_ratio": vol_ratio, "ret_now": ret_now, "ret_prior": ret_prior}}


def score_volume_price_bottom(df: pd.DataFrame, vol_short: int = 5, vol_long: int = 20,
                               price_short: int = 5) -> dict:
    """量價背離（抄底用，max 15）：量縮價增＝賣壓輕、籌碼安定（惜售），打底/賣壓竭盡訊號。"""
    max_score = 15
    if df is None or len(df) < max(vol_long, 2 * price_short + 1):
        return {"score": 0, "max": max_score, "label": "量價 ⚪ 資料不足",
                "note": "量縮價增＝賣壓竭盡訊號（規則式，未回測校準，僅供參考）", "sub": {}}
    vol_ratio = _vol_ratio(df, vol_short, vol_long)
    ret = _price_ret_chg(df, price_short)
    if vol_ratio is None or ret is None:
        return {"score": 0, "max": max_score, "label": "量價 ⚪ 資料不足",
                "note": "量縮價增＝賣壓竭盡訊號（規則式，未回測校準，僅供參考）", "sub": {}}
    ret_now, ret_prior = ret["ret_now"], ret["ret_prior"]

    if vol_ratio <= 0.6 and ret_now > 0:
        score, label = 15, f"量價 🟢 量縮{vol_ratio:.1f}x＋價守穩（賣壓竭盡/惜售）"
    elif vol_ratio <= 0.75 and ret_now >= 0:
        score, label = 8, f"量價 🟡 量縮{vol_ratio:.1f}x＋價持穩"
    else:
        score, label = 0, f"量價 ⚪ 量{vol_ratio:.1f}x（無背離）"

    return {"score": score, "max": max_score, "label": label,
            "note": "量縮價增＝賣壓竭盡訊號（規則式，未回測校準，僅供參考）",
            "sub": {"vol_ratio": vol_ratio, "ret_now": ret_now, "ret_prior": ret_prior}}


def _score_structure(df: pd.DataFrame, lookback: int, order: int, kind: str) -> dict:
    """結構轉折共用核心（max 10）。kind='top'（逃頂：前高未過＝結構轉弱）或
    'bottom'（抄底：前低未破＝結構轉強），骨架比照 core/divergence.py 的
    `_detect(kind=...)` 兩層薄 wrapper 手法，避免 top/bottom 鏡像複製。"""
    max_score = 10
    struct = detect_swing_structure(df, lookback=lookback, order=order)
    is_top = kind == "top"
    note = ("前高未過/結構轉弱＝頭部警訊（規則式，未回測校準，僅供參考）" if is_top
            else "前低未破/結構轉強＝底部訊號（規則式，未回測校準，僅供參考）")
    if struct["structure"] is None:
        return {"score": 0, "max": max_score, "label": "結構 ⚪ 資料不足", "note": note, "sub": {}}

    if is_top:
        if struct["structure"] == "mixed" and struct["higher_low"] is True:
            score, label = 10, "結構 🟠 前高未過（結構轉弱）"
        elif struct["structure"] == "LH_LL":
            score, label = 6, "結構 🔴 空頭結構延續（前高前低皆走低）"
        else:
            score, label = 0, "結構 ⚪ 無轉弱訊號"
    else:
        if struct["structure"] == "mixed" and struct["higher_high"] is False:
            score, label = 10, "結構 🟢 前低未破（結構轉強）"
        elif struct["structure"] == "HH_HL":
            score, label = 6, "結構 🟢 多頭結構延續（前高前低皆墊高）"
        else:
            score, label = 0, "結構 ⚪ 無轉強訊號"

    return {"score": score, "max": max_score, "label": label, "note": note, "sub": struct}


def score_structure_top(df: pd.DataFrame, lookback: int = 120, order: int = 4) -> dict:
    """結構轉折（逃頂用，max 10）：前高未過（結構轉弱）／空頭結構延續視為頭部警訊。"""
    return _score_structure(df, lookback, order, "top")


def score_structure_bottom(df: pd.DataFrame, lookback: int = 120, order: int = 4) -> dict:
    """結構轉折（抄底用，max 10）：前低未破（結構轉強）／多頭結構延續視為底部訊號。"""
    return _score_structure(df, lookback, order, "bottom")
