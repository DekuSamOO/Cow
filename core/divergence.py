"""
core/divergence.py
價格 vs 動能指標（RSI / MACD）背離偵測 — 純 pandas/numpy，無 Streamlit 依賴

頂背離（看跌）：價格創更高的高點（HH），但指標卻是更低的高點（LH）
  → 價格堆高但動能衰竭，為強弩之末（來源筆記第二段「技術分析之功能衰竭」）。
底背離（看漲）：價格創更低的低點（LL），但指標更高的低點（HL）。

供 core/relative_high（頂背離）與未來抄底（底背離）共用。
"""
from typing import Optional, Dict, Any
import numpy as np
import pandas as pd


def _local_extrema(vals: np.ndarray, order: int, kind: str) -> list:
    """
    回傳嚴格局部極值的位置 index 清單。
    kind='high' 找局部高點；'low' 找局部低點。
    嚴格：須為 [i-order, i+order] 窗內唯一極值（避免平台誤判）。
    """
    n = len(vals)
    out = []
    for i in range(order, n - order):
        c = vals[i]
        if np.isnan(c):
            continue
        window = vals[i - order:i + order + 1]
        if np.isnan(window).all():
            continue
        if kind == "high":
            if c == np.nanmax(window) and np.sum(window == c) == 1:
                out.append(i)
        else:
            if c == np.nanmin(window) and np.sum(window == c) == 1:
                out.append(i)
    return out


def detect_swing_structure(df: pd.DataFrame, lookback: int = 120, order: int = 4,
                            price_high_col: str = "high", price_low_col: str = "low") -> Dict[str, Any]:
    """
    波段結構偵測（公開函數）：用 `_local_extrema` 各自抓 high/low 欄位的近期樞紐，
    比較最後兩個高點（h1 較舊／h2 較新）與最後兩個低點（l1 較舊／l2 較新），判斷：
      HH_HL：h2>h1 且 l2>l1（前高後高、前低後低皆墊高）→ 多頭結構延續。
      LH_LL：h2<=h1 且 l2<=l1（前高後高、前低後低皆走低）→ 空頭結構延續。
      mixed：一個 higher 一個不是 → 結構轉換中，可能是頭部/底部轉折訊號
             （由呼叫端依 higher_high/higher_low 進一步判讀是「前高未過」還是「前低未破」）。

    只回傳原始 pivot 年齡（last_high_pivot_age / last_low_pivot_age，距今幾根 K 棒）；
    年齡過大代表訊號已陳舊、效力應打折，但衰減邏輯留給呼叫端處理，這裡不做。

    資料不足（df 太短、缺欄位、pivot 不足 2 個）時，所有欄位回 None，不 raise。
    """
    empty = {
        "structure": None, "last_high": None, "prior_high": None,
        "last_low": None, "prior_low": None,
        "higher_high": None, "higher_low": None,
        "last_high_pivot_age": None, "last_low_pivot_age": None,
    }
    if df is None or df.empty or price_high_col not in df.columns or price_low_col not in df.columns:
        return empty

    sub = df.tail(lookback)
    highs = sub[price_high_col].to_numpy(dtype=float)
    lows = sub[price_low_col].to_numpy(dtype=float)

    high_pivots = _local_extrema(highs, order, "high")
    low_pivots = _local_extrema(lows, order, "low")
    if len(high_pivots) < 2 or len(low_pivots) < 2:
        return empty

    h1_idx, h2_idx = high_pivots[-2], high_pivots[-1]
    l1_idx, l2_idx = low_pivots[-2], low_pivots[-1]
    h1, h2 = float(highs[h1_idx]), float(highs[h2_idx])
    l1, l2 = float(lows[l1_idx]), float(lows[l2_idx])

    higher_high = bool(h2 > h1)
    higher_low = bool(l2 > l1)

    if higher_high and higher_low:
        structure = "HH_HL"
    elif (not higher_high) and (not higher_low):
        structure = "LH_LL"
    else:
        structure = "mixed"

    n = len(sub)
    return {
        "structure": structure,
        "last_high": h2, "prior_high": h1,
        "last_low": l2, "prior_low": l1,
        "higher_high": higher_high, "higher_low": higher_low,
        "last_high_pivot_age": int(n - 1 - h2_idx),
        "last_low_pivot_age": int(n - 1 - l2_idx),
    }


def _detect(df: pd.DataFrame, lookback: int, order: int, indicator: str,
            kind: str, recent_bars: int) -> Dict[str, Any]:
    """頂/底背離共用核心。kind='top' 或 'bottom'。"""
    empty = {
        "has_divergence": False, "strength": 0.0, "indicator": indicator,
        "kind": kind, "last_pivot_age": None,
        "price_change_pct": None, "indicator_change": None,
        "confirm_lag": int(order),   # 樞紐須右側 order 根更極端才確認 → 結構性確認延遲（非即時）
    }
    if df is None or df.empty or indicator not in df.columns:
        return empty

    sub = df.tail(lookback)
    price_col = "high" if (kind == "top" and "high" in sub.columns) else \
                ("low" if (kind == "bottom" and "low" in sub.columns) else "close")
    if price_col not in sub.columns or sub[indicator].notna().sum() < 5:
        return empty

    price = sub[price_col].values.astype(float)
    ind = sub[indicator].values.astype(float)
    ex_kind = "high" if kind == "top" else "low"
    pivots = _local_extrema(price, order, ex_kind)
    if len(pivots) < 2:
        return empty

    i2 = pivots[-1]                          # 最後一個樞紐
    p2, d2 = price[i2], ind[i2]
    last_age = len(price) - 1 - i2          # 最後樞紐距今幾根 K 棒
    if np.isnan(d2) or p2 == 0 or last_age > recent_bars:
        return empty                         # 無近期樞紐 → 無「當前」背離

    # 由近而遠搜尋前一個「價格已被突破但動能更弱」的樞紐（regular divergence 正規定義），
    # 比「固定取相鄰兩樞紐」穩健，不因樞紐取樣密度而漏抓。
    i1 = None
    for cand in reversed(pivots[:-1]):
        p1c, d1c = price[cand], ind[cand]
        if np.isnan(d1c) or p1c == 0:
            continue
        if kind == "top":
            cond = (p2 > p1c) and (d2 < d1c)     # 價 HH、指標 LH
        else:
            cond = (p2 < p1c) and (d2 > d1c)     # 價 LL、指標 HL
        if cond:
            i1 = cand
            break

    has = i1 is not None
    p1 = price[i1] if has else price[pivots[-2]]
    d1 = ind[i1] if has else ind[pivots[-2]]

    price_change_pct = (p2 / p1 - 1) * 100 if p1 else 0.0
    indicator_change = d2 - d1

    strength = 0.0
    if has:
        # 強度：價格背離幅度（%）與指標反向幅度的綜合，clamp 0-1
        price_term = abs(price_change_pct) / 5.0      # 5% 視為飽和
        ind_term = abs(indicator_change) / 10.0       # 指標差 10（RSI 點/同尺度）視為飽和
        strength = float(min(1.0, 0.5 * price_term + 0.5 * ind_term))

    return {
        "has_divergence": bool(has),
        "strength": round(strength, 3),
        "indicator": indicator,
        "kind": kind,
        "last_pivot_age": int(last_age),
        "price_change_pct": round(float(price_change_pct), 2),
        "indicator_change": round(float(indicator_change), 3),
        "confirm_lag": int(order),   # 背離已確認，但落後轉折約 order 根 K 棒（防 repaint 的代價）
    }


def detect_top_divergence(df: pd.DataFrame, lookback: int = 120, order: int = 4,
                          indicator: str = "RSI_14",
                          recent_bars: int = 25) -> Dict[str, Any]:
    """價格 HH 但指標 LH 的頂背離（看跌）。indicator 預設日線 RSI_14，亦可傳 'MACD'。"""
    return _detect(df, lookback, order, indicator, "top", recent_bars)


def detect_bottom_divergence(df: pd.DataFrame, lookback: int = 120, order: int = 4,
                             indicator: str = "RSI_14",
                             recent_bars: int = 25) -> Dict[str, Any]:
    """價格 LL 但指標 HL 的底背離（看漲）。"""
    return _detect(df, lookback, order, indicator, "bottom", recent_bars)


def _detect_combo(detect_fn, df: pd.DataFrame, lookback: int, order: int,
                  recent_bars: int) -> Dict[str, Any]:
    """頂/底 combo 共用核心：對 RSI_14 + MACD 各跑一次 detect_fn 再彙整。
    MACD 欄位優先用 'MACD'（indicators.py 設的別名），無則退 'MACD_Hist'，皆無則略。"""
    rsi = detect_fn(df, lookback, order, "RSI_14", recent_bars)
    macd_col = "MACD" if (df is not None and "MACD" in df.columns) else \
               ("MACD_Hist" if (df is not None and "MACD_Hist" in df.columns) else None)
    macd = (detect_fn(df, lookback, order, macd_col, recent_bars)
            if macd_col else {"has_divergence": False, "strength": 0.0})
    n_confirm = int(rsi["has_divergence"]) + int(macd["has_divergence"])
    return {
        "has_divergence": n_confirm > 0,
        "strength": max(rsi["strength"], macd["strength"]),
        "n_confirm": n_confirm,
        "confirm_lag": int(order),   # 結構性確認延遲（約 order 根 K 棒），UI/推播可標「落後 N 根」
        "rsi": rsi,
        "macd": macd,
    }


def detect_top_divergence_combo(df: pd.DataFrame, lookback: int = 120,
                                order: int = 4, recent_bars: int = 25) -> Dict[str, Any]:
    """綜合 RSI_14 與 MACD 兩指標的頂背離（看跌），回傳
    {has_divergence, strength, n_confirm, rsi, macd}；n_confirm 為出現背離的指標數（0-2）。"""
    return _detect_combo(detect_top_divergence, df, lookback, order, recent_bars)


def detect_bottom_divergence_combo(df: pd.DataFrame, lookback: int = 120,
                                   order: int = 4, recent_bars: int = 25) -> Dict[str, Any]:
    """綜合 RSI_14 與 MACD 兩指標的底背離（看漲），鏡像 detect_top_divergence_combo。
    供 core/relative_low（底部技術回穩維度）與抄底警報共用。"""
    return _detect_combo(detect_bottom_divergence, df, lookback, order, recent_bars)
