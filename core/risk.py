"""
core/risk.py
ATR 風控框架 — 純函數，無 Streamlit/網路依賴

出自「技術指標的真實用途」筆記最被低估的實戰點：**停損位置用 ATR（1.5–3×）而非拍腦袋、
用支撐壓力判風險報酬比**。ATR 不預測方向，只界定「正常波動」尺度 → 停損別設在會被正常
波動掃掉的位置。

單一真實來源：watcher（BTC_WATCH.py／watcher.py）與 LINE 推播（service/notification/builders.py）
共用 compute_atr_risk()，避免公式在兩邊分別維護後漂移。
"""
import math


def compute_atr_risk(df, price, support=None, lookback=60, k=2.0):
    """ATR 停損 + 支撐壓力風報比。回傳 dict，資料不足回 None。

    {atr, atr_pct, k, stop_long, stop_short, support, support_pct,
     resistance, resistance_pct, lookback, reward_risk}
    reward_risk 僅在近 lookback 日仍有上檔空間（resistance > price）時給值，否則 None。
    """
    if df is None or getattr(df, "empty", True) or "ATR" not in getattr(df, "columns", []) \
       or len(df) < 20 or not price or price <= 0:
        return None
    atr = float(df["ATR"].iloc[-1])
    if math.isnan(atr) or atr <= 0:
        return None
    atr_pct = atr / price * 100
    stop_long, stop_short = price - k * atr, price + k * atr
    hh = float(df["high"].tail(lookback).max())           # 近 lookback 日壓力（前高）
    ll = float(df["low"].tail(lookback).min())             # 近 lookback 日支撐
    sup = support if (support and support > 0) else ll     # BTC 用動態地板、其餘用近期低
    reward, risk = hh - price, k * atr
    reward_risk = reward / risk if reward > 0 and risk > 0 else None
    return {
        "atr": atr, "atr_pct": atr_pct, "k": k,
        "stop_long": stop_long, "stop_short": stop_short,
        "support": sup, "support_pct": (sup / price - 1) * 100,
        "resistance": hh, "resistance_pct": (hh / price - 1) * 100,
        "lookback": lookback, "reward_risk": reward_risk,
    }


def atr_risk_rows(df, price, support=None, lookback=60, k=2.0) -> list:
    """watcher 終端顯示用：格式化字串列。回傳 0–2 列；資料不足回 []。"""
    r = compute_atr_risk(df, price, support=support, lookback=lookback, k=k)
    if r is None:
        return []
    rows = [
        f"  風控框架      ATR(14) ${r['atr']:,.0f} ({r['atr_pct']:.1f}%/日)"
        f"｜{r['k']:g}×ATR 停損：多 ${r['stop_long']:,.0f} / 空 ${r['stop_short']:,.0f}",
        f"  風報參考      支撐 ${r['support']:,.0f} ({r['support_pct']:+.1f}%)"
        f"｜壓力(近{r['lookback']}日高) ${r['resistance']:,.0f} ({r['resistance_pct']:+.1f}%)",
    ]
    if r["reward_risk"] is not None:
        rows[1] += f"｜多方風報 1:{r['reward_risk']:.1f}"
    return rows
