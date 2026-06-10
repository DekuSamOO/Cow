"""
core/trend_direction.py  ·  v1.0
趨勢方向（波段雷達第三軸）— 單一真實來源，純 pandas/numpy，**不依賴 Streamlit**

波段雷達原有「逃頂（貴不貴）＋抄底（便宜不便宜）」兩條相對估值量表，本模組補上正交的
第三軸「**目前風往哪吹**」：多因子綜合判斷日線趨勢方向。供 dashboard、BTC_WATCH.py
（path import 本檔）、LINE 推播共用，杜絕邏輯漂移。

設計（鏡像 core/relative_low.py 的結構，但分數為「有號」而非 0–100）：
  四維加權 → 淨方向分 net ∈ [-100, +100]，看多為正、看空為負。

  | 維度          | 權重 | 內容                                                    |
  |---------------|------|--------------------------------------------------------|
  | 均線結構       | ±40 | close / SMA_50 / SMA_200 排列（多頭排列→+40，完全空頭→-40）|
  | MACD 動能      | ±30 | 0 軸上下 × 金叉/死叉（MACD vs Signal）                    |
  | 斜率動能       | ±15 | SMA_200_Slope 標準化 %（+ SMA_50 近 20 日斜率）           |
  | ADX 確信       | ±15 | 前三維方向 × ADX 強度檔                                   |

  ⚠️ ADX < 20（無趨勢）時，方向三維總分先打 0.6 折再加 ADX(=0)，避免均線假突破在盤整盤
     被放大成強趨勢訊號。這是把「趨勢是否確立」內生到分數，而非只在標籤註記。

  趨勢方向與逃頂/抄底**正交**：可同時「強多頭 + 逃頂高」（過熱續漲，順勢但留意止盈），
  也可「空頭 + 抄底高」（深跌便宜但仍在下行，勿純憑估值接刀）。三軸合看才完整。

本層零網路請求：所有欄位取自呼叫端算好的日線（indicators + bear_bottom 後）。
"""
import math
from typing import Optional, Dict, Tuple

import pandas as pd


# ══════════════════════════════════════════════════════════════════════════════
# 常數（單一來源；BTC_WATCH.py path import 直接取用，杜絕兩邊閾值漂移）
# ══════════════════════════════════════════════════════════════════════════════

# 四維權重（各維方向分上限的絕對值；總和 100）
WEIGHTS_TREND = {
    "ma_structure": 40,   # 一、均線排列（close/SMA_50/SMA_200）← 趨勢骨架
    "macd":         30,   # 二、MACD 動能（0 軸 + 金叉死叉）
    "slope":        15,   # 三、長均線斜率（SMA_200 + SMA_50 斜率）
    "adx":          15,   # 四、ADX 趨勢強度（確認前三維方向）
}

# ADX 強度檔位（趨勢強度，非方向）
ADX_NO_TREND   = 20.0   # < 20：無趨勢（盤整）→ 方向分打折、ADX 維度給 0
ADX_MODERATE   = 25.0   # 20–25：弱趨勢
ADX_STRONG     = 40.0   # 25–40：中趨勢；≥40：強趨勢

# ADX < 20 時，方向三維（均線/MACD/斜率）總分的折扣係數（趨勢未確立）
WEAK_TREND_DISCOUNT = 0.6

# SMA_50 vs SMA_200 視為「糾結」的相對差距（百分比）
MA_TANGLE_PCT = 1.0


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _get(row, key):
    """從 Series/dict 取值，缺失或 NaN 回傳 None。"""
    v = row.get(key) if hasattr(row, "get") else None
    return None if _nan(v) else float(v)


# ══════════════════════════════════════════════════════════════════════════════
# 四維子評分（皆回傳有號分數：看多為正、看空為負）
# ══════════════════════════════════════════════════════════════════════════════

def _score_ma_structure(row) -> dict:
    """均線結構（±40）= close / SMA_50 / SMA_200 排列。趨勢骨架，權重最高。"""
    close = _get(row, "close")
    sma50 = _get(row, "SMA_50")
    sma200 = _get(row, "SMA_200")

    if close is None or sma200 is None:
        return {"value": "—", "score": 0, "max": WEIGHTS_TREND["ma_structure"],
                "label": "⚪ 累積中(需 200 日)", "note": "close / SMA_50 / SMA_200 多空排列",
                "sub": {"close": close, "sma50": sma50, "sma200": sma200}}

    above200 = close > sma200
    # SMA_50 缺失時退化為僅用 SMA_200（前 50 日）
    if sma50 is None:
        if above200: s, lbl = 24, "🟢 站上 200 日均（SMA_50 累積中）"
        else:        s, lbl = -24, "🔴 跌破 200 日均（SMA_50 累積中）"
    else:
        tangle = abs(sma50 - sma200) / sma200 * 100 < MA_TANGLE_PCT
        above50 = close > sma50
        bull_stack = close > sma50 > sma200      # 完全多頭排列
        bear_stack = close < sma50 < sma200      # 完全空頭排列
        if bull_stack:
            s, lbl = 40, "🟢 完全多頭排列 (價>SMA50>SMA200)"
        elif bear_stack:
            s, lbl = -40, "🔴 完全空頭排列 (價<SMA50<SMA200)"
        elif tangle:
            # 均線糾結 → 趨勢未明，方向由價格相對位置給小幅傾向
            s, lbl = (8, "⚪ 均線糾結偏多") if above200 else (-8, "⚪ 均線糾結偏空")
        elif above200 and above50:
            s, lbl = 28, "🟢 多頭 (價站上雙均，SMA 未黃金交叉)"
        elif above200:
            s, lbl = 16, "🟡 偏多 (價>SMA200，回踩 SMA50)"
        elif above50:
            s, lbl = -16, "🟡 偏空 (價<SMA200，反彈過 SMA50)"
        else:
            s, lbl = -28, "🔴 空頭 (價跌破雙均，SMA 未死亡交叉)"

    cross = ""
    if sma50 is not None and sma200 is not None:
        cross = "黃金交叉" if sma50 > sma200 else "死亡交叉"
    return {
        "value": f"價 {close:,.0f}｜SMA50 {sma50:,.0f}｜SMA200 {sma200:,.0f}" if sma50
                 else f"價 {close:,.0f}｜SMA200 {sma200:,.0f}",
        "score": s, "max": WEIGHTS_TREND["ma_structure"], "label": lbl,
        "note": f"均線排列（{cross}）" if cross else "均線排列",
        "sub": {"close": close, "sma50": sma50, "sma200": sma200,
                "golden_cross": (None if sma50 is None else sma50 > sma200)},
    }


def _score_macd(row) -> dict:
    """MACD 動能（±30）= 0 軸上下 × MACD vs Signal（金叉/死叉）。"""
    macd = _get(row, "MACD")
    sig = _get(row, "MACD_Signal")
    hist = _get(row, "MACD_Hist")

    if macd is None or sig is None:
        return {"value": "—", "score": 0, "max": WEIGHTS_TREND["macd"],
                "label": "⚪ 累積中", "note": "MACD 零軸位置 + 金叉/死叉",
                "sub": {"macd": macd, "signal": sig, "hist": hist}}

    above_zero = macd > 0
    golden = macd > sig          # MACD 在訊號線上方 = 金叉狀態
    if above_zero and golden:
        s, lbl = 30, "🟢 零軸上金叉 (強多動能)"
    elif above_zero:
        s, lbl = 12, "🟡 零軸上死叉 (多頭轉弱)"
    elif golden:
        s, lbl = -12, "🟡 零軸下金叉 (空頭轉弱)"
    else:
        s, lbl = -30, "🔴 零軸下死叉 (強空動能)"

    h_txt = f"｜柱 {hist:+.0f}" if hist is not None else ""
    return {
        "value": f"MACD {macd:+.0f}｜訊號 {sig:+.0f}{h_txt}",
        "score": s, "max": WEIGHTS_TREND["macd"], "label": lbl,
        "note": "MACD(12,26,9) 零軸位置 + 與訊號線金叉/死叉",
        "sub": {"macd": macd, "signal": sig, "hist": hist,
                "above_zero": above_zero, "golden": golden},
    }


def _score_slope(row, df) -> dict:
    """斜率動能（±15）= SMA_200 斜率(標準化 %) + SMA_50 近 20 日斜率。"""
    sma200 = _get(row, "SMA_200")
    slope200_raw = _get(row, "SMA_200_Slope")   # indicators 已算 diff(20)（價格單位）
    # 標準化為相對 %：20 日 SMA_200 變化占當前 SMA_200 的百分比
    slope200_pct = None
    if slope200_raw is not None and sma200 not in (None, 0):
        slope200_pct = slope200_raw / sma200 * 100

    if slope200_pct is None:
        p_s, p_lbl, p_val = 0, "⚪ 累積中", "—"
    else:
        p_val = f"{slope200_pct:+.1f}%"
        if   slope200_pct >= 3.0:  p_s, p_lbl = 10, "🟢 SMA200 強升"
        elif slope200_pct >= 0.5:  p_s, p_lbl = 6,  "🟢 SMA200 走升"
        elif slope200_pct > -0.5:  p_s, p_lbl = 0,  "⚪ SMA200 走平"
        elif slope200_pct > -3.0:  p_s, p_lbl = -6, "🔴 SMA200 走降"
        else:                      p_s, p_lbl = -10, "🔴 SMA200 強降"

    # SMA_50 近 20 日斜率（df 可選；缺則略過此子項）
    s50_s, s50_lbl = 0, ""
    if df is not None and "SMA_50" in getattr(df, "columns", []):
        s50 = df["SMA_50"].dropna()
        if len(s50) >= 21 and s50.iloc[-21] not in (0, None) and not math.isnan(s50.iloc[-21]):
            chg = (s50.iloc[-1] - s50.iloc[-21]) / s50.iloc[-21] * 100
            if   chg >= 1.0:  s50_s, s50_lbl = 5, "SMA50 升"
            elif chg > -1.0:  s50_s, s50_lbl = 0, "SMA50 平"
            else:             s50_s, s50_lbl = -5, "SMA50 降"

    score = max(-WEIGHTS_TREND["slope"], min(WEIGHTS_TREND["slope"], p_s + s50_s))
    return {
        "value": f"SMA200 {p_val}" + (f"｜{s50_lbl}" if s50_lbl else ""),
        "score": score, "max": WEIGHTS_TREND["slope"],
        "label": p_lbl + (f"；{s50_lbl}" if s50_lbl else ""),
        "note": "長均線斜率（趨勢動能方向）",
        "sub": {"sma200_slope_pct": slope200_pct, "sma200_slope_score": p_s,
                "sma50_slope_score": s50_s},
    }


def _adx_strength(adx: Optional[float]) -> Tuple[int, str]:
    """ADX 強度檔位 → (強度分 0/5/10/15, 標籤)。不含方向。"""
    if adx is None:
        return 0, "⚪ 無資料"
    if   adx >= ADX_STRONG:   return 15, f"🟢 強趨勢 (ADX {adx:.0f})"
    if   adx >= ADX_MODERATE: return 10, f"🟢 中趨勢 (ADX {adx:.0f})"
    if   adx >= ADX_NO_TREND: return 5,  f"🟡 弱趨勢 (ADX {adx:.0f})"
    return 0, f"⚪ 無趨勢/盤整 (ADX {adx:.0f})"


def _score_adx(row, dir_sign: int) -> dict:
    """
    ADX 確信（±15）= ADX 強度 × 前三維方向 sign。
    ADX 不決定方向，只確認/放大前三維已給出的方向；無趨勢時給 0。
    """
    adx = _get(row, "ADX")
    strength, lbl = _adx_strength(adx)
    score = strength * (1 if dir_sign > 0 else -1 if dir_sign < 0 else 0)
    return {
        "value": f"ADX {adx:.0f}" if adx is not None else "—",
        "score": score, "max": WEIGHTS_TREND["adx"], "label": lbl,
        "note": "ADX 趨勢強度（確認前三維方向；<20 視為盤整不計）",
        "sub": {"adx": adx, "strength": strength, "dir_sign": dir_sign},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 綜合評分（dashboard / script / LINE 共用單一入口）
# ══════════════════════════════════════════════════════════════════════════════

def compute_trend_score(
    row, df: Optional[pd.DataFrame] = None,
) -> Tuple[int, Dict[str, dict]]:
    """
    日線趨勢方向四維綜合評分。鏡像 relative_low.compute_relative_low_score，
    但分數為**有號**淨方向分 net ∈ [-100, +100]（看多為正、看空為負）。

    回傳 (net:int, signals:dict[dim] = {value,score,max,label,note,sub})。

    row：最新日線（含 close / SMA_50 / SMA_200 / SMA_200_Slope / MACD /
         MACD_Signal / MACD_Hist / ADX，需先過 core.indicators）。
    df ：完整日線（SMA_50 近 20 日斜率用，可選）。
    """
    ma = _score_ma_structure(row)
    macd = _score_macd(row)
    slope = _score_slope(row, df)

    # 前三維方向 sign（決定 ADX 維度該往哪邊加）
    dir_raw = ma["score"] + macd["score"] + slope["score"]
    dir_sign = 1 if dir_raw > 0 else -1 if dir_raw < 0 else 0
    adx = _score_adx(row, dir_sign)

    # ADX < 20（無趨勢）→ 方向三維打折，避免盤整中的均線假突破被放大
    adx_val = adx["sub"].get("adx")
    weak = adx_val is not None and adx_val < ADX_NO_TREND
    dir_total = dir_raw * (WEAK_TREND_DISCOUNT if weak else 1.0)

    net = int(round(dir_total + adx["score"]))
    net = max(-100, min(100, net))

    signals = {"ma_structure": ma, "macd": macd, "slope": slope, "adx": adx}
    return net, signals


def trend_meta(net: int) -> Tuple[str, str, str]:
    """(方向等級, 顏色, 操作意涵) — 有號淨方向分 → 五態。"""
    if net >= 50:
        return "🟢 強多頭趨勢", "#00cc88", "順勢做多為主，回踩均線找買點；逃頂分高時分批止盈"
    if net >= 20:
        return "🟢 多頭趨勢", "#00aa66", "偏多操作，趨勢未破不輕易翻空"
    if net > -20:
        return "⚪ 盤整/無明確趨勢", "#9e9e9e", "區間操作，等待方向選擇；勿追突破"
    if net > -50:
        return "🔴 空頭趨勢", "#ff8800", "偏空操作，反彈減碼；抄底分高也勿純憑估值接刀"
    return "🔴 強空頭趨勢", "#ff4b4b", "順勢偏空/觀望，趨勢未轉不搶反彈"


def compute_trend_direction(row, df: Optional[pd.DataFrame] = None) -> dict:
    """趨勢方向完整評估（淨方向分 + 等級）。所有資料由呼叫端注入（本層零網路請求）。"""
    net, signals = compute_trend_score(row, df)
    level, color, action = trend_meta(net)
    return {
        "trend_score":   net,        # 有號 [-100, +100]
        "trend_level":   level,
        "trend_color":   color,
        "trend_action":  action,
        "trend_signals": signals,
    }
