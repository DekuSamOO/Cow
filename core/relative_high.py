"""
core/relative_high.py  ·  v1.0
相對高點（逃頂雷達）— 單一真實來源，純 pandas/numpy，**不依賴 Streamlit**

來源：Permanent Note「BTC相對高點判斷」（2026/5 月 $82k→$60k 事後論）。
鏡像 core/bear_bottom.py（底部側），供 dashboard、Crypto/BTC_WATCH.py（path import
本檔）、未來 LINE 推播共用，杜絕邏輯漂移。

兩層輸出：
  Layer A 短中期逃頂警報（0–100）：合約過熱 + 技術衰竭 + 鏈上派發 + 情緒過熱 + 總經逆風。
    對應筆記五維，給「止盈/開空」用。compute_escape_top_score()。
  Layer B 長週期大頂：複用 bear_bottom.calculate_market_cycle_score_breakdown 的 bull_total
    + 四季論秋季。compute_cycle_top_state()。

另含高點價位錨 compute_cycle_top_estimates()（Pi Cycle 頂 / Mayer 頂 / 冪律上界 / 四季論牛頂）。

⚠️ 維度權重狀態：資金費率/技術衰竭/情緒/總經為「可回測擬合」維度（見
   tests/relative_high_backtest.py）；OI 與 ETF 因歷史不足（OI 自建快照、ETF 僅 2024+），
   權重為**專家設定（未擬合）**，介面以 UNFITTED_DIMS 標示。
"""
import math
from typing import Optional, Dict, Any, List, Tuple

import numpy as np
import pandas as pd

from core.divergence import detect_top_divergence_combo
from core.bear_bottom import calculate_market_cycle_score_breakdown
from core.season_forecast import forecast_price, get_current_season


# ══════════════════════════════════════════════════════════════════════════════
# 常數（單一來源；BTC_WATCH.py path import 直接取用，杜絕兩邊閾值漂移）
# ══════════════════════════════════════════════════════════════════════════════

# 資金費率年化門檻（2026-06 以幣安資費史回歸重校，見 tests/funding_threshold_calib.py）：
# 後60日最大回撤在年化≥30% 由 ~-10% 翻倍至 ~-18%（轉折），≥50% 飽和(~-19%)，≥70% 未更深；
# 年化≥90% 僅 35 日(1.76%)且全在 2021 狂熱、不更準 → 滿分線由舊 90% 下修至 50%、過熱起點由 50% 下修至 30%。
FUNDING_ANN_YELLOW = 30.0    # 年化 % — 過熱起點（回撤轉折；黃）
FUNDING_ANN_RED    = 50.0    # 年化 % — 極端/滿分（回撤飽和；紅）

# Layer A 五維權重（各維最高分；總和 100）
WEIGHTS = {
    "derivatives": 30,   # 一、合約過熱（資金費率 20 + OI 10）
    "technical":   25,   # 二、技術衰竭（頂背離 18 + RSI 超買 7）
    "onchain":     20,   # 三、鏈上派發（ETF 連續流出 12 + SOPR 8）
    "sentiment":   15,   # 四、情緒過熱（F&G 10 + BTC.D 輪動 5）
    "macro":       10,   # 五、總經逆風（通膨/就業 hawkish + 事件臨近）
}

# 權重未經回測擬合的維度（歷史資料不足；介面需標示）
UNFITTED_DIMS = ("onchain",)   # OI 無歷史、ETF 僅 2024+ → onchain 維度視為未擬合


def annualize_funding(rate_8h_pct: Optional[float]) -> Optional[float]:
    """每 8h 資金費率(%) → 年化(%)。每日結算 3 次 × 365 天。"""
    if rate_8h_pct is None or (isinstance(rate_8h_pct, float) and math.isnan(rate_8h_pct)):
        return None
    return rate_8h_pct * 3 * 365


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


# ══════════════════════════════════════════════════════════════════════════════
# Layer A — 五維子評分
# ══════════════════════════════════════════════════════════════════════════════

def _score_derivatives(funding_8h, oi_stats) -> dict:
    """合約過熱（max 30）= 資金費率年化(20) + OI 相對高(10)。"""
    ann = annualize_funding(funding_8h)
    # 資金費率年化（max 20）
    if ann is None:
        f_s, f_lbl, f_val = 0, "⚪ 無資料", "—"
    else:
        f_val = f"{ann:.0f}% (年化)"
        # 階梯由 funding_threshold_calib.py 回歸：滿分 ≥50%（回撤飽和）、轉折 ≥30%（回撤翻倍）
        if   ann >= FUNDING_ANN_RED:    f_s, f_lbl = 20, "🔴 極端過熱 (≥50% 年化, 回撤飽和)"
        elif ann >= 40:                 f_s, f_lbl = 17, "🔴 嚴重過熱 (≥40%)"
        elif ann >= FUNDING_ANN_YELLOW: f_s, f_lbl = 14, "🟠 過熱 (≥30%, 回撤轉折)"
        elif ann >= 20:                 f_s, f_lbl = 6,  "🟡 偏熱 (≥20%)"
        elif ann >= 12:                 f_s, f_lbl = 2,  "⚪ 偏多 (≥12%)"
        elif ann >= 0:                  f_s, f_lbl = 0,  "⚪ 中性"
        else:                           f_s, f_lbl = 0,  "🟢 空方付費 (負費率)"

    # OI 相對高（max 10）— 依自建快照歷史分位（不足則 0 + 累積中）
    if not oi_stats or oi_stats.get("percentile") is None:
        o_s, o_lbl, o_val = 0, "⚪ 歷史累積中", "—"
    else:
        pct = oi_stats["percentile"]
        o_val = f"{pct:.0f} 分位"
        if oi_stats.get("is_near_high") or pct >= 95: o_s, o_lbl = 10, "🔴 近期新高 (槓桿過載)"
        elif pct >= 85: o_s, o_lbl = 7, "🟠 相對高位"
        elif pct >= 70: o_s, o_lbl = 4, "🟡 偏高"
        else:           o_s, o_lbl = 0, "⚪ 中性"

    return {
        "value": f"資費 {f_val}｜OI {o_val}",
        "score": f_s + o_s, "max": WEIGHTS["derivatives"],
        "label": f"資費 {f_lbl}；OI {o_lbl}",
        "note": "資金費率年化（筆記閾值 50/90%）+ OI 自建快照分位",
        "sub": {"funding_ann": ann, "funding_score": f_s,
                "oi_percentile": (oi_stats or {}).get("percentile"), "oi_score": o_s},
    }


def _score_technical(row, df) -> dict:
    """技術衰竭（max 25）= 頂背離(18) + RSI_14 超買(7)。"""
    div = detect_top_divergence_combo(df) if df is not None else {"has_divergence": False,
                                                                  "n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    strength = div.get("strength", 0.0)
    if   n >= 2: d_s, d_lbl = 18, "🔴 RSI+MACD 雙頂背離"
    elif n == 1: d_s, d_lbl = round(8 + 4 * strength), "🟠 單指標頂背離"
    else:        d_s, d_lbl = 0, "⚪ 無頂背離"

    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi):
        r_s, r_lbl, r_val = 0, "⚪ 無資料", "—"
    else:
        r_val = f"{rsi:.0f}"
        if   rsi >= 80: r_s, r_lbl = 7, "🔴 極度超買 (≥80)"
        elif rsi >= 75: r_s, r_lbl = 5, "🟠 超買 (≥75)"
        elif rsi >= 70: r_s, r_lbl = 3, "🟡 偏超買 (≥70)"
        else:           r_s, r_lbl = 0, "⚪ 中性"

    return {
        "value": f"背離×{n}｜RSI {r_val}",
        "score": d_s + r_s, "max": WEIGHTS["technical"],
        "label": f"{d_lbl}；RSI {r_lbl}",
        "note": "日線頂背離（價HH/指標LH）+ RSI_14 超買",
        "sub": {"divergence_n": n, "divergence_strength": strength,
                "divergence_score": d_s, "rsi": (None if _nan(rsi) else float(rsi)),
                "rsi_score": r_s},
    }


def _score_onchain(etf_summary, sopr) -> dict:
    """鏈上派發（max 20）= ETF 連續淨流出(12) + SOPR 飆高(8)。未擬合維度。"""
    # ETF 連續淨流出天數（筆記：連續 13 天淨流出為強訊號）
    if not etf_summary or etf_summary.get("n", 0) == 0:
        e_s, e_lbl, e_val = 0, "⚪ 無資料", "—"
    else:
        days = etf_summary.get("consecutive_outflow_days", 0)
        e_val = f"連{days}日流出"
        if   days >= 10: e_s, e_lbl = 12, "🔴 連續流出≥10日 (機構撤退)"
        elif days >= 7:  e_s, e_lbl = 10, "🔴 連續流出≥7日"
        elif days >= 5:  e_s, e_lbl = 7,  "🟠 連續流出≥5日"
        elif days >= 3:  e_s, e_lbl = 4,  "🟡 連續流出≥3日"
        elif days >= 1:  e_s, e_lbl = 2,  "⚪ 流出 1-2 日"
        else:            e_s, e_lbl = 0,  "🟢 淨流入"

    # SOPR（>1 獲利了結；飆高 = 老韭菜/巨鯨倒貨）
    if _nan(sopr):
        s_s, s_lbl, s_val = 0, "⚪ 無資料", "—"
    else:
        s_val = f"{sopr:.3f}"
        if   sopr >= 1.08: s_s, s_lbl = 8, "🔴 SOPR 飆高 (大量獲利了結)"
        elif sopr >= 1.05: s_s, s_lbl = 6, "🟠 SOPR 偏高"
        elif sopr >= 1.03: s_s, s_lbl = 4, "🟡 SOPR 微高"
        elif sopr >= 1.01: s_s, s_lbl = 2, "⚪ 小幅獲利了結"
        else:              s_s, s_lbl = 0, "⚪ 中性/虧損賣出"

    return {
        "value": f"ETF {e_val}｜SOPR {s_val}",
        "score": e_s + s_s, "max": WEIGHTS["onchain"],
        "label": f"ETF {e_lbl}；{s_lbl}",
        "note": "⚠️ 未擬合：ETF 連續淨流出(Farside) + SOPR(bitcoin-data)",
        "sub": {"etf_consecutive_outflow": (etf_summary or {}).get("consecutive_outflow_days"),
                "etf_score": e_s, "sopr": (None if _nan(sopr) else float(sopr)),
                "sopr_score": s_s},
    }


def _score_sentiment(fng, btc_d_trend) -> dict:
    """情緒過熱（max 15）= F&G 極貪(10) + BTC.D 下降輪動(5)。"""
    if _nan(fng):
        g_s, g_lbl, g_val = 0, "⚪ 無資料", "—"
    else:
        g_val = f"{fng:.0f}"
        if   fng >= 90: g_s, g_lbl = 10, "🔴 極度貪婪 (≥90)"
        elif fng >= 80: g_s, g_lbl = 8,  "🟠 貪婪 (≥80)"
        elif fng >= 75: g_s, g_lbl = 5,  "🟡 偏貪 (≥75)"
        elif fng >= 70: g_s, g_lbl = 3,  "⚪ 偏多 (≥70)"
        else:           g_s, g_lbl = 0,  "⚪ 中性/恐懼"

    if not btc_d_trend or btc_d_trend.get("change_pp") is None:
        b_s, b_lbl, b_val = 0, "⚪ 累積中", "—"
    else:
        chg = btc_d_trend["change_pp"]
        b_val = f"{chg:+.1f}pp"
        if   btc_d_trend.get("is_falling") or chg <= -1.0: b_s, b_lbl = 5, "🟠 BTC.D 下降 (山寨輪動)"
        elif chg <= -0.5:                                  b_s, b_lbl = 3, "🟡 BTC.D 偏弱"
        else:                                              b_s, b_lbl = 0, "⚪ BTC.D 穩定/上升"

    return {
        "value": f"F&G {g_val}｜BTC.D {b_val}",
        "score": g_s + b_s, "max": WEIGHTS["sentiment"],
        "label": f"{g_lbl}；{b_lbl}",
        "note": "恐懼貪婪極貪 + BTC.D 下降（資金末端輪動）",
        "sub": {"fng": (None if _nan(fng) else float(fng)), "fng_score": g_s,
                "btcd_change_pp": (btc_d_trend or {}).get("change_pp"), "btcd_score": b_s},
    }


def _score_macro(macro) -> dict:
    """
    總經逆風（max 10）= 通膨/就業 hawkish(7) + 事件臨近(3)。
    macro dict（皆選填）：
      cpi_hot / pce_hot : bool  通膨升溫（高於前期）
      jobs_strong       : bool  就業強勁（升息傾向）
      event_within_days : int   最近一個重大數據/FOMC 距今天數
    升息環境抽走高風險資產流動性 → BTC 逆風（來源筆記第五段）。
    """
    if not macro:
        return {"value": "—", "score": 0, "max": WEIGHTS["macro"],
                "label": "⚪ 無資料", "note": "通膨/就業 hawkish + 事件臨近（Notion 行事曆）",
                "sub": {}}
    h_s = 0
    bits = []
    if macro.get("cpi_hot") or macro.get("pce_hot"):
        h_s += 4; bits.append("通膨升溫")
    if macro.get("jobs_strong"):
        h_s += 3; bits.append("就業強勁")
    h_s = min(h_s, 7)

    ev = macro.get("event_within_days")
    if ev is not None and ev <= 1:   e_s, ev_lbl = 3, "重大數據 ≤1日"
    elif ev is not None and ev <= 3: e_s, ev_lbl = 2, "重大數據 ≤3日"
    elif ev is not None and ev <= 7: e_s, ev_lbl = 1, "重大數據 ≤7日"
    else:                            e_s, ev_lbl = 0, "無臨近事件"
    if e_s: bits.append(ev_lbl)

    return {
        "value": "｜".join(bits) if bits else "中性",
        "score": h_s + e_s, "max": WEIGHTS["macro"],
        "label": ("🟠 " + "、".join(bits)) if bits else "⚪ 中性",
        "note": "通膨/就業 hawkish + 事件臨近（Notion 行事曆）",
        "sub": {"hawkish_score": h_s, "event_score": e_s,
                "event_within_days": ev},
    }


def compute_escape_top_score(
    row, df: Optional[pd.DataFrame] = None, *,
    funding_8h: Optional[float] = None,
    oi_stats: Optional[dict] = None,
    etf_summary: Optional[dict] = None,
    sopr: Optional[float] = None,
    fng: Optional[float] = None,
    btc_d_trend: Optional[dict] = None,
    macro: Optional[dict] = None,
) -> Tuple[int, Dict[str, dict]]:
    """
    Layer A 逃頂綜合評分（0–100）。鏡像 bear_bottom.calculate_bear_bottom_score。
    回傳 (score:int, signals:dict[dim] = {value,score,max,label,note,sub})。

    row：最新日線（含 RSI_14 等技術欄位，pd.Series 或 dict-like）。
    df ：完整日線（背離偵測用）。其餘為各資料服務算好的純量/dict（呼叫端注入，
         本層不做任何網路請求 → 易測、可被 BTC_WATCH 以自抓資料餵入）。
    """
    signals = {
        "derivatives": _score_derivatives(funding_8h, oi_stats),
        "technical":   _score_technical(row, df),
        "onchain":     _score_onchain(etf_summary, sopr),
        "sentiment":   _score_sentiment(fng, btc_d_trend),
        "macro":       _score_macro(macro),
    }
    score = int(sum(s["score"] for s in signals.values()))
    score = max(0, min(100, score))
    return score, signals


def escape_top_meta(score: int) -> Tuple[str, str, str]:
    """(等級, 顏色, 操作建議) — 鏡像 _bear_score_meta 的反向。"""
    if score >= 75:
        return "🔴 強烈逃頂訊號", "#ff4b4b", "高度過熱，分批止盈／考慮對沖空單"
    if score >= 60:
        return "🟠 明確過熱", "#ff8800", "減倉、收緊移動止盈"
    if score >= 45:
        return "🟡 偏熱警戒", "#ffcc00", "停止加倉、提高警覺"
    if score >= 25:
        return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🟢 無過熱", "#00cc88", "無逃頂壓力"


# ══════════════════════════════════════════════════════════════════════════════
# Layer B — 長週期大頂（複用既有 bull_total + 四季論秋季）
# ══════════════════════════════════════════════════════════════════════════════

def compute_cycle_top_state(row, df: Optional[pd.DataFrame], price: float) -> dict:
    """
    長週期頂部狀態。複用 bear_bottom.calculate_market_cycle_score_breakdown 的牛頂分數，
    結合四季論季節（秋季=泡沫破裂）。回傳 dict。
    """
    market_score, bear_total, bull_total, rows = calculate_market_cycle_score_breakdown(row)
    season = get_current_season()
    fc = forecast_price(price, df=df) if df is not None else None
    is_autumn = bool(season and season["season"] in ("autumn",))
    eff_season = fc["effective_season"]["season"] if fc else (season["season"] if season else None)
    return {
        "market_score":   market_score,     # 牛頂 − 熊底
        "bull_total":     bull_total,        # 0–100 牛頂分數
        "bear_total":     bear_total,
        "breakdown":      rows,
        "time_season":    season["season"] if season else None,
        "effective_season": eff_season,
        "is_autumn":      is_autumn or eff_season == "autumn",
        "month_in_cycle": season["month_in_cycle"] if season else None,
        "forecast":       fc,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 高點價位錨（鏡像 bottom_floors）
# ══════════════════════════════════════════════════════════════════════════════

def compute_cycle_top_estimates(price: float, df: Optional[pd.DataFrame]) -> List[dict]:
    """
    高點價位錨（由高到低排序）：
      - Pi Cycle 頂訊號線：2×SMA350（SMA111 上穿即歷史頂）
      - Mayer 頂：SMA730 × 2.4
      - 冪律上界：PowerLaw_Support × 10^0.45（≈2.82×）
      - 四季論牛頂：forecast_price 牛市目標（僅牛市分支有）
    回傳 list[{label, value, kind, note}]，無資料的項略過。
    """
    out: List[dict] = []
    if df is None or df.empty:
        return out
    last = df.iloc[-1]

    sma350x2 = last.get("SMA_350x2")
    if not _nan(sma350x2) and sma350x2 > 0:
        out.append({"label": "Pi Cycle 頂訊號線", "value": float(sma350x2),
                    "kind": "technical", "note": "2×SMA350，SMA111 上穿=歷史大頂"})

    sma730 = last.get("SMA_730")
    if not _nan(sma730) and sma730 > 0:
        out.append({"label": "Mayer 頂", "value": float(sma730) * 2.4,
                    "kind": "technical", "note": "2年均線 ×2.4（Mayer 頂閾值）"})

    pl_support = last.get("PowerLaw_Support")
    if not _nan(pl_support) and pl_support > 0:
        out.append({"label": "冪律上界", "value": float(pl_support) * (10 ** 0.45),
                    "kind": "anchor", "note": "冪律中線 ×2.82（走廊上緣）"})

    fc = forecast_price(price, df=df)
    if fc and fc.get("forecast_type") == "bull_peak":
        if fc.get("target_high"):
            out.append({"label": "四季論牛頂(樂觀)", "value": float(fc["target_high"]),
                        "kind": "season", "note": "四季論牛市目標高點"})
        if fc.get("target_median"):
            out.append({"label": "四季論牛頂(中位)", "value": float(fc["target_median"]),
                        "kind": "season", "note": "四季論牛市中位目標"})

    out.sort(key=lambda x: -x["value"])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 整合（dashboard / script 共用單一入口）
# ══════════════════════════════════════════════════════════════════════════════

def compute_relative_high(
    price: float, row, df: Optional[pd.DataFrame] = None, *,
    funding_8h: Optional[float] = None,
    oi_stats: Optional[dict] = None,
    etf_summary: Optional[dict] = None,
    sopr: Optional[float] = None,
    fng: Optional[float] = None,
    btc_d_trend: Optional[dict] = None,
    macro: Optional[dict] = None,
) -> dict:
    """
    相對高點完整評估（Layer A + Layer B + 價位錨）。
    所有外部資料由呼叫端注入（本層零網路請求），dashboard 與 BTC_WATCH 共用。
    """
    score, signals = compute_escape_top_score(
        row, df, funding_8h=funding_8h, oi_stats=oi_stats, etf_summary=etf_summary,
        sopr=sopr, fng=fng, btc_d_trend=btc_d_trend, macro=macro)
    level, color, action = escape_top_meta(score)
    return {
        "escape_score":   score,
        "escape_level":   level,
        "escape_color":   color,
        "escape_action":  action,
        "escape_signals": signals,
        "cycle_top":      compute_cycle_top_state(row, df, price),
        "top_estimates":  compute_cycle_top_estimates(price, df),
        "unfitted_dims":  list(UNFITTED_DIMS),
    }
