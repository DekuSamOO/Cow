"""
core/relative_high_tw.py  ·  v0.1（絕對值起步，未擬合）
台股相對高點（逃頂雷達）— 純函數、零網路請求。鏡像 core/relative_high 結構，
把加密專屬維度（funding/OI/鏈上）替換為台股對應（融資融券/三大法人/TDCC/PE-PB）。

五維（0–100）：
  技術衰竭 30（頂背離 + RSI 超買）   ← 複用既有 divergence/RSI（通用）
  法人派發 25（三大法人賣超／外資大賣）← 替 ETF 流出
  槓桿過熱 20（融資餘額增速高＝散戶追高）← 替 funding/OI 過熱
  估值過高 15（PE/PB 絕對值高）     ← 替 cycle
  籌碼鬆動 10（TDCC 散戶持股比高/大戶降）← 集保派發

⚠️ 同 relative_low_tw：v0.1 絕對值起步〔未擬合〕，法人以近20日均量正規化，
   閾值為專家起點（鏡像 tw_stock_climber），待回測校準。
"""
import math
from typing import Optional, Dict, Tuple

from core.divergence import detect_top_divergence_combo

WEIGHTS_HIGH_TW = {
    "technical": 30, "institution": 25, "leverage": 20, "valuation": 15, "tdcc": 10,
}
UNFITTED_DIMS_HIGH_TW = ("institution", "leverage", "valuation", "tdcc")


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _avg_vol(df) -> Optional[float]:
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < 5:
        return None
    v = float(df["volume"].tail(20).mean())
    return v if v > 0 else None


def _score_technical_high(row, df) -> dict:
    """技術衰竭（max 30）= 頂背離(20) + RSI_14 超買(10)。複用既有 divergence（通用）。"""
    div = detect_top_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    if n >= 2: d_s, d_l = 20, "🔴 RSI+MACD 雙頂背離"
    elif n == 1: d_s, d_l = round(9 + 5 * div.get("strength", 0.0)), "🟠 單指標頂背離"
    else: d_s, d_l = 0, "⚪ 無頂背離"
    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi): r_s, r_l = 0, "⚪ RSI 無"
    elif rsi >= 80: r_s, r_l = 10, f"🔴 RSI {rsi:.0f} 極度超買"
    elif rsi >= 70: r_s, r_l = 6, f"🟠 RSI {rsi:.0f} 超買"
    else: r_s, r_l = 0, f"⚪ RSI {rsi:.0f}"
    return {"score": d_s + r_s, "max": 30, "label": f"{d_l}；{r_l}",
            "note": "頂背離（價HH/指標LH）+ RSI 超買", "sub": {"div_n": n, "rsi": (None if _nan(rsi) else float(rsi))}}


def _score_institution_high(institutional, df) -> dict:
    """法人派發（max 25）= 三大法人賣超（以近 20 日均量正規化）。"""
    if not institutional or institutional.get("total_net") is None:
        return {"score": 0, "max": 25, "label": "⚪ 無法人資料",
                "note": "三大法人賣超＝派發〔未擬合〕", "sub": {}}
    net = institutional["total_net"]
    av = _avg_vol(df)
    ratio = (net / av * 100) if av else None
    if ratio is None:
        s, l = (10, "🟠 法人賣超（無量基準）") if net < 0 else (0, "⚪ 法人買超/平")
    elif ratio <= -20: s, l = 25, f"🔴 法人大賣 {ratio:+.0f}%均量"
    elif ratio <= -8: s, l = 16, f"🟠 法人賣超 {ratio:+.0f}%均量"
    elif ratio <= -3: s, l = 7, f"🟡 法人小賣 {ratio:+.0f}%均量"
    else: s, l = 0, f"⚪ 法人 {ratio:+.0f}%均量（買/平）"
    return {"score": s, "max": 25, "label": l, "note": "三大法人買賣超 / 近20日均量（負＝賣超派發）",
            "sub": {"total_net": net, "ratio_pct": ratio}}


def _score_leverage_high(margin) -> dict:
    """槓桿過熱（max 20）= 融資餘額增速高（散戶追高加槓桿）。"""
    if not margin or margin.get("fin_chg_pct") is None:
        return {"score": 0, "max": 20, "label": "⚪ 無融資資料",
                "note": "融資增速高＝散戶追高過熱〔未擬合〕", "sub": {}}
    chg = margin["fin_chg_pct"]
    if chg >= 5: s, l = 20, f"🔴 融資暴增 {chg:+.1f}%（散戶追高）"
    elif chg >= 3: s, l = 14, f"🟠 融資大增 {chg:+.1f}%"
    elif chg >= 1: s, l = 7, f"🟡 融資增 {chg:+.1f}%"
    else: s, l = 0, f"⚪ 融資 {chg:+.1f}%（未過熱）"
    return {"score": s, "max": 20, "label": l, "note": "融資餘額日變化（散戶槓桿過熱）",
            "sub": {"fin_chg_pct": chg}}


def _score_valuation_high(valuation) -> dict:
    """估值過高（max 15）= PE 高(8) + PB 高(7)。絕對值分級〔未擬合〕。"""
    if not valuation:
        return {"score": 0, "max": 15, "label": "⚪ 無估值資料（上櫃/缺）",
                "note": "PE/PB 絕對值〔未擬合〕", "sub": {}}
    pe, pb = valuation.get("pe"), valuation.get("pb")
    if _nan(pe) or pe <= 0: pe_s, pe_l = 0, "⚪ PE 無/負"
    elif pe >= 40: pe_s, pe_l = 8, f"🔴 PE {pe:.0f} 偏貴(≥40)"
    elif pe >= 25: pe_s, pe_l = 4, f"🟡 PE {pe:.0f} 偏高(≥25)"
    else: pe_s, pe_l = 0, f"⚪ PE {pe:.0f}"
    if _nan(pb) or pb <= 0: pb_s, pb_l = 0, "⚪ PB 無"
    elif pb >= 5: pb_s, pb_l = 7, f"🔴 PB {pb:.1f} 偏貴(≥5)"
    elif pb >= 3: pb_s, pb_l = 3, f"🟡 PB {pb:.1f} 偏高(≥3)"
    else: pb_s, pb_l = 0, f"⚪ PB {pb:.1f}"
    return {"score": pe_s + pb_s, "max": 15, "label": f"{pe_l}；{pb_l}",
            "note": "PE/PB 絕對值過高〔未擬合，待分位/回測〕",
            "sub": {"pe": pe, "pb": pb}}


def _score_tdcc_high(tdcc) -> dict:
    """籌碼鬆動（max 10）= TDCC 散戶持股比高（籌碼分散＝派發末端）。"""
    if not tdcc or tdcc.get("retail_pct") is None:
        return {"score": 0, "max": 10, "label": "⚪ 無集保資料",
                "note": "散戶持股比高＝籌碼鬆動〔未擬合〕", "sub": {}}
    rp = tdcc["retail_pct"]
    if rp >= 40: s, l = 10, f"🔴 散戶 {rp:.0f}%（籌碼鬆散）"
    elif rp >= 25: s, l = 5, f"🟡 散戶 {rp:.0f}%"
    else: s, l = 0, f"⚪ 散戶 {rp:.0f}%（集中）"
    return {"score": s, "max": 10, "label": l, "note": "TDCC 散戶（≤50張）持股比",
            "sub": {"retail_pct": rp, "major_pct": tdcc.get("major_pct")}}


def compute_relative_high_tw(row, df=None, *, chip=None) -> Tuple[int, Dict[str, dict]]:
    """台股相對高點五維評分（0–100）。chip = service.tw_chip.get_chip_bundle 結果（可缺）。"""
    chip = chip or {}
    signals = {
        "technical": _score_technical_high(row, df),
        "institution": _score_institution_high(chip.get("institutional"), df),
        "leverage": _score_leverage_high(chip.get("margin")),
        "valuation": _score_valuation_high(chip.get("valuation")),
        "tdcc": _score_tdcc_high(chip.get("tdcc")),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_high_tw_meta(score: int) -> Tuple[str, str, str]:
    if score >= 65: return "🔴 強烈逃頂", "#ff4b4b", "技術＋籌碼俱過熱，分批止盈/減碼"
    if score >= 45: return "🟠 明確過熱", "#ff8800", "減碼、收緊移動止盈"
    if score >= 30: return "🟡 偏熱警戒", "#ffcc00", "停止加倉、提高警覺"
    if score >= 15: return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🟢 無過熱", "#00cc88", "無逃頂壓力"
