"""
core/bottom_floors.py
最低價綜合評估 — 單一真實來源（LINE 推播 + dashboard 共用，杜絕兩邊漂移）
─────────────────────────────────────────────────────────────────────
彙整所有「最低價/底部地板」算法為單一 dict：
  四季論趨勢底（season_forecast.project_bear_bottom，v1.4 趨勢外插）
  + 4 個既有 floor：200週均線 / 冪律下界 / 礦工電費 / 礦工 all-in
  + on-chain 錨：Realized Price / Balanced Price / CVDD（bitcoin-data.com）
  + 技術錨：Mayer 底（SMA730×0.6）/ AHR999 抄底頂（0.45）
  + 礦工成本隱含底（電費 × 歷史熊底/電費 中位數）

並輸出：
  final_low      最終最低價估計 = max(四季論趨勢底, 礦工電費硬地板)
                 （歷史三輪熊底從未跌破純電費；electricity = 硬地板）
  ensemble_low   多強錨中位數（穩健中央估計）

實證基礎（core/miner_cost 回測 2015/2018/2022）：
  熊底/電費 = 1.98→1.10→1.06x（收斂，從未跌破）；熊底/all-in = 1.24/0.69/0.67x（牛末跌破 all-in）

純資料/計算層，無 Streamlit 依賴。
"""
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import math
import numpy as np
import pandas as pd

from core.season_forecast import project_bear_bottom
from core import miner_cost

# 可調參數集中於 config.py（單一可調來源）；此處保留既有內部名稱，下游零改動
from config import (BOTTOM_RELIABILITY as _RELIABILITY,
                    MINER_BOTTOM_MULT as _MINER_BOTTOM_MULT,
                    MAYER_BOTTOM_RATIO as _MAYER_BOTTOM_RATIO,
                    AHR999_DCA_CEIL as _AHR999_DCA_CEIL)

_GENESIS = datetime(2009, 1, 3)


def _weighted_median(pairs):
    """pairs = [(value, weight)]；回傳加權中位數（累積權重跨 50% 處）。"""
    pairs = sorted((v, w) for v, w in pairs if v and w)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    half, cum = total / 2.0, 0.0
    for v, w in pairs:
        cum += w
        if cum >= half:
            return v
    return pairs[-1][0]


def _powerlaw_median(days: float) -> float:
    return 10 ** (-17.01467 + 5.84 * math.log10(max(days, 1)))


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if df is not None and not df.empty and getattr(df.index, "tz", None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df


def _sma(df: pd.DataFrame, n: int) -> Optional[float]:
    if df is None or df.empty or "close" not in df.columns or len(df) < n:
        return None
    v = float(df["close"].rolling(n).mean().iloc[-1])
    return v if not math.isnan(v) else None


def _ma200w(df: pd.DataFrame) -> Optional[float]:
    """200 週均線（日線重採樣為週收盤後 SMA200），與 daily_line_notify 一致。"""
    if df is None or df.empty or "close" not in df.columns:
        return None
    try:
        weekly = df["close"].resample("W").last().dropna()
        if len(weekly) >= 200:
            return float(weekly.tail(200).mean())
    except Exception:
        pass
    return None


def compute_all_bottom_estimates(
    current_price: float,
    df: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
    hashrate_ths: Optional[float] = None,
    onchain: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    彙整全部最低價算法。

    參數：
      df            BTC 日線（需 close；index 為 DatetimeIndex）
      hashrate_ths  目前全網算力（TH/s）；None 時礦工成本項為 None
      onchain       service.bottom_metrics.get_latest_bottom_metrics() 結果；
                    None 時呼叫端可不帶（on-chain 項為 None）
    回傳見模組 docstring。
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.replace(tzinfo=None)   # 對齊 season_forecast 的 naive datetime（HALVING_DATES）
    df = _strip_tz(df)

    # ── 1. 四季論趨勢底（單一來源）──
    season = project_bear_bottom(current_price, df, now)

    # ── 2. 技術/數學 floor ──
    days = (now - _GENESIS).days
    pl_med = _powerlaw_median(days)
    pl_floor = pl_med * (10 ** -0.45)            # 冪律下界
    ma200w = _ma200w(df)                          # 200 週均線
    sma200 = _sma(df, 200)
    sma730 = _sma(df, 730)
    mayer_floor = sma730 * _MAYER_BOTTOM_RATIO if sma730 else None
    # AHR999 = (P/SMA200)×(P/PL) = P²/(SMA200×PL)；解 AHR999=0.45 的 P（抄底區上界）
    ahr_floor = (math.sqrt(_AHR999_DCA_CEIL * sma200 * pl_med)
                 if (sma200 and pl_med) else None)

    # ── 3. 礦工成本（即時，用當前算力）──
    miner_elec = miner_allin = miner_implied = None
    if hashrate_ths and hashrate_ths > 0:
        miner_elec  = miner_cost.electricity_breakeven(hashrate_ths, now)
        miner_allin = miner_cost.all_in_cost(hashrate_ths, now)
        miner_implied = miner_elec * _MINER_BOTTOM_MULT

    # ── 4. on-chain 錨 ──
    oc = onchain or {}
    realized = oc.get("realized_price")
    balanced = oc.get("balanced_price")
    cvdd     = oc.get("cvdd")

    # ── 組裝 estimates 列表（一算法一列）──
    estimates: List[Dict[str, Any]] = []

    def add(key, label, value, kind, note=""):
        if value is not None and value == value and value > 0:   # 過濾 None/NaN/0
            estimates.append({"key": key, "label": label, "value": float(value),
                              "kind": kind, "note": note,
                              "reliability": _RELIABILITY.get(key, 50)})

    if season:
        add("season_bottom", "四季論趨勢底", season["bottom_mid"], "season",
            f"bottom_mult {season['bottom_mult_point']:.3f} × {season['ath_ref_label']}")
    add("miner_elec",   "礦工電費(硬地板)", miner_elec,   "floor",
        "歷史三輪熊底從未跌破純電費")
    add("miner_implied","礦工成本隱含底",  miner_implied, "anchor",
        f"電費 × {_MINER_BOTTOM_MULT}（歷史熊底/電費中位數）")
    add("realized",     "Realized Price",  realized,      "anchor", "全網成本基礎")
    add("balanced",     "Balanced Price",  balanced,      "anchor", "歷史大底錨")
    add("cvdd",         "CVDD",            cvdd,          "anchor", "歷史絕對底部")
    add("ma200w",       "200 週均線",      ma200w,        "floor",  "歷史牛熊分界")
    add("mayer_floor",  "Mayer 底",        mayer_floor,   "anchor", "2年線 × 0.6")
    add("ahr999_floor", "AHR999 抄底頂",   ahr_floor,     "anchor", "AHR999=0.45 對應價")
    add("power_law",    "冪律下界",        pl_floor,      "floor",  "冪律 -0.45 log 通道")
    add("miner_allin",  "礦工 all-in(警示)", miner_allin, "warning",
        "含折舊/場地；牛末熊底常跌破至 ~0.67×")

    # ── final_low：四季論趨勢底，但不低於礦工電費硬地板（1b）──
    season_mid = season["bottom_mid"] if season else None
    floor_candidates = [v for v in (season_mid, miner_elec) if v]
    if floor_candidates:
        final_low = max(floor_candidates)
        final_low_basis = ("礦工電費硬地板" if (miner_elec and final_low == miner_elec)
                           else "四季論趨勢底")
    else:
        final_low = None
        final_low_basis = None

    # ── ensemble：可靠度加權中位數（穩健中央估計；排除 all-in 警示線）──
    ensemble_low = _weighted_median(
        [(e["value"], e["reliability"]) for e in estimates if e["key"] != "miner_allin"]
    )

    return {
        "current_price":   current_price,
        "asof":            oc.get("asof"),
        "season_bottom":   season,
        "estimates":       estimates,
        "final_low":       final_low,
        "final_low_basis": final_low_basis,
        "ensemble_low":    ensemble_low,
        # 便利欄（display 用）
        "miner_elec":      miner_elec,
        "miner_allin":     miner_allin,
    }
