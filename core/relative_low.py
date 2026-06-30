"""
core/relative_low.py  ·  v1.0
相對底部（抄底雷達）— 單一真實來源，純 pandas/numpy，**不依賴 Streamlit**

鏡像 core/relative_high.py（逃頂側），供 dashboard、Crypto/BTC_WATCH.py（path import
本檔）、未來 LINE 抄底推播共用，杜絕邏輯漂移。

六維逃底評分（0–100），權重經 tests/relative_low_backtest.py 敏感度測試（鏡像逃頂版方法）
拍板，與逃頂**非對稱**——實證顯示底部側「長週期深跌」判別力最強（AUC 0.662），
故給最高權重；頂部側則以「合約過熱」為首。這個非對稱本身即實證發現：
底部靠「估值便宜」（長週期指標），頂部靠「槓桿過熱」（合約指標）。

⚠️ 維度權重狀態：
  - cycle（長週期深跌）/ technical / sentiment 為可回測維度（見 backtest）。
  - onchain（SOPR）2026-06 敏感度驗證通過：單維方向正確（AUC 0.585）、加入合成無害且隨
    onchain 權重單調有益、門檻命中穩定（tests/relative_low_backtest.py::validate_unfitted_dims）
    → 已移出 UNFITTED；ETF 子項僅 2024+ 資料薄，沿用專家權重。
  - macro 拆兩子維：event-window（事件臨近）為規則式、永久不可統計擬合 → RULE_BASED_DIMS_LOW；
    dovish flags（通膨/就業）可擬合但無歷史源（FRED 被擋）→ 待回補（PENDING_FIT_SUBDIMS_LOW）。
    UNFITTED_DIMS_LOW 因此清空（onchain 已驗、macro 改規則式分類）。
  - derivatives 負費率子項 2026-06 已回歸重校門檻（AUC 0.626，判別帶在淺負；見 funding_threshold_calib）。
"""
import math
from typing import Optional, Dict, Any, Tuple

import numpy as np
import pandas as pd

from core.divergence import detect_bottom_divergence_combo
from core.relative_high import annualize_funding


# ══════════════════════════════════════════════════════════════════════════════
# 常數（單一來源；BTC_WATCH.py path import 直接取用，杜絕兩邊閾值漂移）
# ══════════════════════════════════════════════════════════════════════════════

# 負資金費率年化門檻（2026-06 回歸重校，見 tests/funding_threshold_calib.py）：
# 負費率→底 AUC 0.626，但判別力在「淺負」(Youden 最佳 ≤-3%)；≤-15/-20/-30% 歷史僅 8/4/3 日(危機)，
# 召回崩到 3% → 大分不該鎖在深負。主判別帶下移至 -2~-5%、滿分線上修至 -20%（仍給危機級洗盤最高分）。
FUNDING_ANN_LOW_YELLOW = -5.0    # 年化 % — 明顯空方付費（主判別帶；黃）
FUNDING_ANN_LOW_RED    = -20.0   # 年化 % — 極端空方付費（危機級洗盤；紅）

# 六維權重（各維最高分；總和 100）— 經 relative_low_backtest 拍板（實證導向）
WEIGHTS_LOW = {
    "cycle":       25,   # 一、長週期深跌（Mayer 10 + 200週 9 + 冪律 6）← AUC 最強
    "derivatives": 20,   # 二、合約超冷（負費率 10 + OI 滾動清洗 10）
    "technical":   20,   # 三、技術回穩（底背離 14 + RSI 超賣 6）
    "sentiment":   15,   # 四、情緒恐慌（F&G 10 + BTC.D 上升 5）
    "onchain":     10,   # 五、鏈上吸籌（ETF 連續流入 6 + SOPR 割肉 4）灰燈
    "macro":       10,   # 六、總經順風（降息/鴿派 7 + 事件臨近 3）灰燈
}

# 維度狀態標示（兩種不同性質，介面以不同 tag 呈現）：
#   UNFITTED_DIMS_LOW   ＝權重採專家設定、歷史樣本不足「待累積後回測」即可擬合（如 OI 自建快照）。
#   RULE_BASED_DIMS_LOW ＝子項本質為規則式、不可統計擬合（macro 的 event-window 事件臨近）。
# onchain：2026-06 敏感度驗證通過，已不在任一清單（見 backtest validate_unfitted_dims）。
# macro：拆兩子維 — event-window(事件臨近)＝規則式(永久不可擬合)；dovish flags(通膨/就業)＝
#        可擬合但目前無歷史源（FRED 公司網路被擋），待雲端/家用網路回補 FRED 後以 backtest 驗證。
UNFITTED_DIMS_LOW = ()
RULE_BASED_DIMS_LOW = ("macro",)
# 待 FRED 歷史回補後可擬合的子項（文件用；非介面 tag 清單）
PENDING_FIT_SUBDIMS_LOW = {"macro": "dovish flags（通膨/就業）待 FRED 歷史回補後回測"}


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


# ══════════════════════════════════════════════════════════════════════════════
# 六維子評分
# ══════════════════════════════════════════════════════════════════════════════

def _score_cycle(row) -> dict:
    """長週期深跌（max 25）= Mayer(10) + 200週均比(9) + 冪律比(6)。實證最強維度。"""
    def _v(k):
        v = row.get(k) if hasattr(row, "get") else None
        return None if _nan(v) else float(v)
    mayer = _v("Mayer_Multiple")
    sma200w = _v("SMA200W_Ratio")
    pl = _v("PowerLaw_Ratio")

    if mayer is None:
        m_s, m_lbl, m_val = 0, "⚪ 累積中(需730日)", "—"
    else:
        m_val = f"{mayer:.2f}x"
        if   mayer < 0.8: m_s, m_lbl = 10, "🟢 低於2年均線×0.8 (極度低估)"
        elif mayer < 1.0: m_s, m_lbl = 6,  "🟡 低於2年均線"
        elif mayer < 1.2: m_s, m_lbl = 3,  "⚪ 略低於均線"
        else:             m_s, m_lbl = 0,  "⚪ 高於均線"

    if sma200w is None:
        s_s, s_lbl, s_val = 0, "⚪ 累積中(需200週)", "—"
    else:
        s_val = f"{sma200w:.2f}x"
        if   sma200w < 1.0: s_s, s_lbl = 9, "🟢 跌破200週均 (歷史絕對底)"
        elif sma200w < 1.3: s_s, s_lbl = 6, "🟡 接近200週均"
        elif sma200w < 2.0: s_s, s_lbl = 3, "⚪ 正常範圍"
        else:               s_s, s_lbl = 0, "⚪ 偏高"

    if pl is None:
        p_s, p_lbl, p_val = 0, "⚪ 累積中", "—"
    else:
        p_val = f"{pl:.1f}x"
        if   pl < 2.0: p_s, p_lbl = 6, "🟢 貼近冪律支撐"
        elif pl < 5.0: p_s, p_lbl = 3, "🟡 略高於冪律支撐"
        else:          p_s, p_lbl = 0, "⚪ 遠高於支撐"

    return {
        "value": f"Mayer {m_val}｜200週 {s_val}｜冪律 {p_val}",
        "score": m_s + s_s + p_s, "max": WEIGHTS_LOW["cycle"],
        "label": f"Mayer {m_lbl}；200週 {s_lbl}；冪律 {p_lbl}",
        "note": "長週期估值深跌（Mayer/200週/冪律）— 敏感度測試 AUC 最強維度",
        "sub": {"mayer": mayer, "mayer_score": m_s, "sma200w": sma200w,
                "sma200w_score": s_s, "powerlaw": pl, "powerlaw_score": p_s},
    }


def _score_derivatives_low(funding_8h, oi_stats) -> dict:
    """合約超冷（max 20）= 負資金費率年化(10) + OI 滾動清洗(10)。"""
    ann = annualize_funding(funding_8h)
    if ann is None:
        f_s, f_lbl, f_val = 0, "⚪ 無資料", "—"
    else:
        f_val = f"{ann:.0f}% (年化)"
        # 階梯由 funding_threshold_calib.py 回歸：判別帶在淺負(≤-2~-5%)，深負(≤-20%)為危機級洗盤滿分
        if   ann <= FUNDING_ANN_LOW_RED:    f_s, f_lbl = 10, "🟢 極端空方付費 (≤-20% 年化, 危機洗盤)"
        elif ann <= -10:                    f_s, f_lbl = 8,  "🟢 嚴重空方付費 (≤-10%)"
        elif ann <= FUNDING_ANN_LOW_YELLOW: f_s, f_lbl = 6,  "🟡 空方付費 (≤-5%)"
        elif ann <= -2:                     f_s, f_lbl = 3,  "🟡 偏空 (≤-2%)"
        elif ann < 0:                       f_s, f_lbl = 1,  "⚪ 微負費率"
        else:                               f_s, f_lbl = 0,  "⚪ 多方付費/中性"

    # OI 滾動清洗（1h 窗 ΔOI，呼叫端以 openInterestHist 算好注入 oi_stats）
    chg = (oi_stats or {}).get("change_1h_pct")
    if chg is None:
        o_s, o_lbl, o_val = 0, "⚪ 無資料", "—"
    else:
        o_val = f"{chg:+.1f}% (1h)"
        if   chg <= -8: o_s, o_lbl = 10, "🟢 強力槓桿清洗 (≤-8%)"
        elif chg <= -5: o_s, o_lbl = 7,  "🟢 槓桿清洗 (≤-5%)"
        elif chg <= -3: o_s, o_lbl = 4,  "🟡 去槓桿 (≤-3%)"
        else:           o_s, o_lbl = 0,  "⚪ 無顯著清洗"

    return {
        "value": f"資費 {f_val}｜OI {o_val}",
        "score": f_s + o_s, "max": WEIGHTS_LOW["derivatives"],
        "label": f"資費 {f_lbl}；OI {o_lbl}",
        "note": "OI 1h 滾動清洗未擬合(歷史不足)；負費率已回歸重校(AUC 0.626，判別帶在淺負)",
        "sub": {"funding_ann": ann, "funding_score": f_s,
                "oi_change_1h_pct": chg, "oi_score": o_s},
    }


def _score_technical_low(row, df) -> dict:
    """技術回穩（max 20）= 底背離 RSI+MACD(14) + RSI_14 超賣(6)。"""
    div = detect_bottom_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    strength = div.get("strength", 0.0)
    if   n >= 2: d_s, d_lbl = 14, "🟢 RSI+MACD 雙底背離"
    elif n == 1: d_s, d_lbl = round(6 + 4 * strength), "🟡 單指標底背離"
    else:        d_s, d_lbl = 0, "⚪ 無底背離"

    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi):
        r_s, r_lbl, r_val = 0, "⚪ 無資料", "—"
    else:
        r_val = f"{rsi:.0f}"
        if   rsi <= 20: r_s, r_lbl = 6, "🟢 極度超賣 (≤20)"
        elif rsi <= 25: r_s, r_lbl = 4, "🟡 超賣 (≤25)"
        elif rsi <= 30: r_s, r_lbl = 2, "⚪ 偏超賣 (≤30)"
        else:           r_s, r_lbl = 0, "⚪ 中性"

    return {
        "value": f"背離×{n}｜RSI {r_val}",
        "score": d_s + r_s, "max": WEIGHTS_LOW["technical"],
        "label": f"{d_lbl}；RSI {r_lbl}",
        "note": "日線底背離（價LL/指標HL）+ RSI_14 超賣",
        "sub": {"divergence_n": n, "divergence_strength": strength,
                "divergence_score": d_s, "rsi": (None if _nan(rsi) else float(rsi)),
                "rsi_score": r_s},
    }


def _score_sentiment_low(fng, btc_d_trend) -> dict:
    """情緒恐慌（max 15）= F&G 極度恐懼(10) + BTC.D 上升避險(5)。"""
    if _nan(fng):
        g_s, g_lbl, g_val = 0, "⚪ 無資料", "—"
    else:
        g_val = f"{fng:.0f}"
        if   fng <= 10: g_s, g_lbl = 10, "🟢 極度恐懼 (≤10)"
        elif fng <= 20: g_s, g_lbl = 8,  "🟢 恐懼 (≤20)"
        elif fng <= 25: g_s, g_lbl = 5,  "🟡 偏恐懼 (≤25)"
        elif fng <= 30: g_s, g_lbl = 3,  "⚪ 偏空 (≤30)"
        else:           g_s, g_lbl = 0,  "⚪ 中性/貪婪"

    if not btc_d_trend or btc_d_trend.get("change_pp") is None:
        b_s, b_lbl, b_val = 0, "⚪ 累積中", "—"
    else:
        chg = btc_d_trend["change_pp"]
        b_val = f"{chg:+.1f}pp"
        if   btc_d_trend.get("is_rising") or chg >= 1.0: b_s, b_lbl = 5, "🟢 BTC.D 上升 (避險回流)"
        elif chg >= 0.5:                                 b_s, b_lbl = 3, "🟡 BTC.D 偏強"
        else:                                            b_s, b_lbl = 0, "⚪ BTC.D 穩定/下降"

    return {
        "value": f"F&G {g_val}｜BTC.D {b_val}",
        "score": g_s + b_s, "max": WEIGHTS_LOW["sentiment"],
        "label": f"{g_lbl}；{b_lbl}",
        "note": "恐懼貪婪極度恐懼 + BTC.D 上升（資金避險回流主鏈）",
        "sub": {"fng": (None if _nan(fng) else float(fng)), "fng_score": g_s,
                "btcd_change_pp": (btc_d_trend or {}).get("change_pp"), "btcd_score": b_s},
    }


def _score_onchain_low(etf_summary, sopr) -> dict:
    """鏈上吸籌（max 10）= ETF 連續淨流入(6) + SOPR 割肉投降(4)。灰燈/未擬合。"""
    if not etf_summary or etf_summary.get("n", 0) == 0:
        e_s, e_lbl, e_val = 0, "⚪ 無資料源", "—"
    else:
        days = etf_summary.get("consecutive_inflow_days", 0)
        e_val = f"連{days}日流入"
        if   days >= 7: e_s, e_lbl = 6, "🟢 連續流入≥7日 (機構吸籌)"
        elif days >= 5: e_s, e_lbl = 4, "🟢 連續流入≥5日"
        elif days >= 3: e_s, e_lbl = 2, "🟡 連續流入≥3日"
        else:           e_s, e_lbl = 0, "⚪ 無顯著流入"

    if _nan(sopr):
        s_s, s_lbl, s_val = 0, "⚪ 無資料源", "—"
    else:
        s_val = f"{sopr:.3f}"
        if   sopr <= 0.92: s_s, s_lbl = 4, "🟢 SOPR 深度割肉 (投降)"
        elif sopr <= 0.95: s_s, s_lbl = 3, "🟢 SOPR 割肉"
        elif sopr <= 0.98: s_s, s_lbl = 2, "🟡 SOPR 微虧賣出"
        else:              s_s, s_lbl = 0, "⚪ 中性/獲利賣出"

    return {
        "value": f"ETF {e_val}｜SOPR {s_val}",
        "score": e_s + s_s, "max": WEIGHTS_LOW["onchain"],
        "label": f"ETF {e_lbl}；{s_lbl}",
        "note": "SOPR 方向驗證 2026-06（AUC 0.585，無害有益）；ETF 連續淨流入 2024+ 資料薄沿用專家權重",
        "sub": {"etf_consecutive_inflow": (etf_summary or {}).get("consecutive_inflow_days"),
                "etf_score": e_s, "sopr": (None if _nan(sopr) else float(sopr)),
                "sopr_score": s_s},
    }


def _score_macro_low(macro) -> dict:
    """
    總經順風（max 10）= 降息/鴿派(7) + 事件臨近(3)。灰燈/未擬合。
    macro dict（皆選填）：cpi_cool / pce_cool / jobs_weak : bool；event_within_days : int。
    降息/流動性寬鬆 → 高風險資產回流 → BTC 順風（逃頂 macro 的反向）。
    """
    if not macro:
        return {"value": "—", "score": 0, "max": WEIGHTS_LOW["macro"],
                "label": "⚪ 無資料源",
                "note": "事件臨近=規則式(不可擬合)；通膨/就業 dovish=待 FRED 回補驗證",
                "sub": {}}
    h_s = 0
    bits = []
    if macro.get("cpi_cool") or macro.get("pce_cool"):
        h_s += 4; bits.append("通膨降溫")
    if macro.get("jobs_weak"):
        h_s += 3; bits.append("就業轉弱")
    h_s = min(h_s, 7)

    ev = macro.get("event_within_days")
    if ev is not None and ev <= 1:   e_s, ev_lbl = 3, "重大數據 ≤1日"
    elif ev is not None and ev <= 3: e_s, ev_lbl = 2, "重大數據 ≤3日"
    elif ev is not None and ev <= 7: e_s, ev_lbl = 1, "重大數據 ≤7日"
    else:                            e_s, ev_lbl = 0, "無臨近事件"
    if e_s: bits.append(ev_lbl)

    return {
        "value": "｜".join(bits) if bits else "中性",
        "score": h_s + e_s, "max": WEIGHTS_LOW["macro"],
        "label": ("🟢 " + "、".join(bits)) if bits else "⚪ 中性",
        "note": "事件臨近=規則式(不可擬合)；通膨/就業 dovish=待 FRED 回補驗證",
        "sub": {"dovish_score": h_s, "event_score": e_s, "event_within_days": ev},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 綜合評分（dashboard / script 共用單一入口）
# ══════════════════════════════════════════════════════════════════════════════

def compute_relative_low_score(
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
    相對底部六維綜合評分（0–100）。鏡像 relative_high.compute_escape_top_score。
    回傳 (score:int, signals:dict[dim] = {value,score,max,label,note,sub})。

    row：最新日線（含 RSI_14 / Mayer_Multiple / SMA200W_Ratio / PowerLaw_Ratio 等，
         需先過 indicators + bear_bottom）。df：完整日線（底背離用）。
    其餘為呼叫端算好的純量/dict（本層零網路請求 → 易測、可被 BTC_WATCH 自抓資料餵入）。
    """
    signals = {
        "cycle":       _score_cycle(row),
        "derivatives": _score_derivatives_low(funding_8h, oi_stats),
        "technical":   _score_technical_low(row, df),
        "sentiment":   _score_sentiment_low(fng, btc_d_trend),
        "onchain":     _score_onchain_low(etf_summary, sopr),
        "macro":       _score_macro_low(macro),
    }
    score = int(sum(s["score"] for s in signals.values()))
    score = max(0, min(100, score))
    return score, signals


def _hash_ribbon_read(hashrate_hist) -> Optional[dict]:
    """Hash Ribbons：30/60 日算力 SMA 交叉（礦工投降→打底）。資料用
    service.bottom_metrics.fetch_hashrate_history_ths()（已快取，零新資料源）。"""
    if not hashrate_hist:
        return None
    try:
        s = pd.Series(hashrate_hist)
        s.index = pd.to_datetime(s.index)
        s = s.sort_index().astype(float)
    except Exception:
        return None
    if len(s) < 60:
        return None
    sma30, sma60 = s.rolling(30).mean(), s.rolling(60).mean()
    a, b = sma30.iloc[-1], sma60.iloc[-1]
    if _nan(a) or _nan(b):
        return None
    cross_up = False
    if len(s) >= 6 and not _nan(sma30.iloc[-6]) and not _nan(sma60.iloc[-6]):
        cross_up = (sma30.iloc[-6] < sma60.iloc[-6]) and (a >= b)
    if cross_up:
        lbl = "🟢 Hash Ribbon 黃金交叉（礦工投降結束，歷史買訊）"
    elif a < b:
        lbl = "🟡 礦工投降中（SMA30<SMA60，打底訊號醞釀）"
    else:
        lbl = "⚪ 算力健康（無礦工投降）"
    return {"value": f"SMA30 {a:.3g}/SMA60 {b:.3g} TH/s", "label": lbl,
            "note": "參考（未計入加權，待回測）；2023/8 有假訊號→須他維確認"}


def reference_low_signals(*, mvrv_z: Optional[float] = None,
                          hashrate_hist=None) -> Dict[str, dict]:
    """抄底側社群參考指標（**不計入 low_score**；啟用加權須先過 relative_low_backtest
    swing-low AUC≥0.55，避免憑社群閾值直接調已校準/已驗證(SOPR)權重）。"""
    out: Dict[str, dict] = {}
    if mvrv_z is not None and not _nan(mvrv_z):
        z = float(mvrv_z)
        if   z <= 0: lbl = "🟢 MVRV-Z≤0 歷史底部區（市值低於成本價）"
        elif z <= 1: lbl = "🟢 MVRV-Z≤1 深度低估"
        elif z <= 2: lbl = "🟡 MVRV-Z≤2 偏低"
        else:        lbl = "⚪ MVRV-Z 中性/偏高"
        out["mvrv_z"] = {"value": f"{z:.2f}", "label": lbl,
                         "note": "參考（未計入加權，待回測 AUC 驗證）"}
    hr = _hash_ribbon_read(hashrate_hist)
    if hr is not None:
        out["hash_ribbon"] = hr
    return out


def relative_low_meta(score: int) -> Tuple[str, str, str]:
    """(等級, 顏色, 操作建議) — 鏡像 escape_top_meta 的反向。"""
    if score >= 75:
        return "🟢 強力抄底訊號", "#00cc88", "高度低估，分批進場／回補空單（需配合動態地板確認）"
    if score >= 60:
        return "🟢 明確低估", "#00aa66", "可開始定投／減空"
    if score >= 45:
        return "🟡 偏冷觀察", "#ffcc00", "留意打底，勿純憑超賣搶反彈"
    if score >= 25:
        return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🔴 無底部訊號", "#ff4b4b", "無低估壓力，勿接刀"


def compute_relative_low(
    price: float, row, df: Optional[pd.DataFrame] = None, *,
    mvrv_z: Optional[float] = None, hashrate_hist=None, **kwargs,
) -> dict:
    """相對底部完整評估（評分 + 等級）。所有外部資料由呼叫端注入（本層零網路請求）。
    mvrv_z / hashrate_hist：社群參考指標，僅顯示於 reference_signals，不計入 low_score（待回測）。"""
    score, signals = compute_relative_low_score(row, df, **kwargs)
    level, color, action = relative_low_meta(score)
    return {
        "low_score":   score,
        "low_level":   level,
        "low_color":   color,
        "low_action":  action,
        "low_signals": signals,
        "unfitted_dims": list(UNFITTED_DIMS_LOW),
        "rule_based_dims": list(RULE_BASED_DIMS_LOW),
        "reference_signals": reference_low_signals(mvrv_z=mvrv_z, hashrate_hist=hashrate_hist),
    }
