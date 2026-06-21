"""
core/relative_low_tw.py  ·  v0.1（絕對值起步，未擬合）
台股相對底部（抄底雷達）— 純函數、零網路請求。鏡像 core/relative_low 結構，
把加密專屬維度（funding/OI/鏈上）替換為台股對應（融資融券/三大法人/TDCC/PE-PB）。

五維（0–100）：
  估值深跌 25（PE 低 + PB 低）        ← 替 cycle 冪律；最強直覺維度
  技術回穩 20（底背離 + RSI 超賣）    ← 複用既有 divergence/RSI（通用）
  槓桿清洗 20（融資大減＝散戶斷頭）   ← 替 OI 清洗
  法人吸籌 20（三大法人買超／外資+投信）← 替 ETF 流入
  大戶吸籌 15（TDCC 大戶持股比高）    ← 集保籌碼

⚠️ v0.1 限制（絕對值起步，使用者拍板）：PE/PB 用**絕對值分級**（非 5 年分位），
   不同產業基準差異大（金融股 PB 1.0 正常、科技股 PB 1.0 偏低）→ 估值維度粗略、標〔未擬合〕。
   法人買賣超以近 20 日均量正規化。閾值為專家起點（鏡像 tw_stock_climber chip/valuation），
   待累積台股歷史後以回測校準（鏡像 relative_low_backtest 方法）。
"""
import math
from typing import Optional, Dict, Tuple

from core.divergence import detect_bottom_divergence_combo

WEIGHTS_LOW_TW = {
    "valuation": 25, "technical": 20, "leverage": 20, "institution": 20, "tdcc": 15,
}
UNFITTED_DIMS_LOW_TW = ("valuation", "leverage", "institution", "tdcc")  # 絕對值起步，全待回測


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _avg_vol(df) -> Optional[float]:
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < 5:
        return None
    v = float(df["volume"].tail(20).mean())
    return v if v > 0 else None


def _score_valuation_low(valuation) -> dict:
    """估值深跌（max 25）= PE 低(13) + PB 低(12)。絕對值分級〔未擬合〕。"""
    if not valuation:
        return {"score": 0, "max": 25, "label": "⚪ 無估值資料（上櫃/缺）",
                "note": "PE/PB 絕對值〔未擬合〕", "sub": {}}
    pe, pb = valuation.get("pe"), valuation.get("pb")
    if _nan(pe) or pe <= 0:
        pe_s, pe_l = 0, "⚪ PE 無/負"
    elif pe < 10: pe_s, pe_l = 13, f"🟢 PE {pe:.0f} 低估(<10)"
    elif pe < 15: pe_s, pe_l = 8, f"🟡 PE {pe:.0f} 偏低(<15)"
    elif pe < 20: pe_s, pe_l = 4, f"⚪ PE {pe:.0f} 合理(<20)"
    else: pe_s, pe_l = 0, f"⚪ PE {pe:.0f} 偏高"
    if _nan(pb) or pb <= 0:
        pb_s, pb_l = 0, "⚪ PB 無"
    elif pb < 1.0: pb_s, pb_l = 12, f"🟢 PB {pb:.2f} 破淨(<1)"
    elif pb < 1.5: pb_s, pb_l = 8, f"🟡 PB {pb:.2f} 偏低(<1.5)"
    elif pb < 2.5: pb_s, pb_l = 3, f"⚪ PB {pb:.2f} 合理(<2.5)"
    else: pb_s, pb_l = 0, f"⚪ PB {pb:.2f} 偏高"
    return {"score": pe_s + pb_s, "max": 25, "label": f"{pe_l}；{pb_l}",
            "note": "PE/PB 絕對值深跌〔未擬合，待分位/回測〕",
            "sub": {"pe": pe, "pb": pb, "pe_score": pe_s, "pb_score": pb_s}}


def _score_technical_low(row, df) -> dict:
    """技術回穩（max 20）= 底背離(14) + RSI_14 超賣(6)。複用既有 divergence（通用）。"""
    div = detect_bottom_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    if n >= 2: d_s, d_l = 14, "🟢 RSI+MACD 雙底背離"
    elif n == 1: d_s, d_l = round(6 + 4 * div.get("strength", 0.0)), "🟡 單指標底背離"
    else: d_s, d_l = 0, "⚪ 無底背離"
    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi): r_s, r_l = 0, "⚪ RSI 無"
    elif rsi <= 20: r_s, r_l = 6, f"🟢 RSI {rsi:.0f} 極度超賣"
    elif rsi <= 30: r_s, r_l = 3, f"🟡 RSI {rsi:.0f} 超賣"
    else: r_s, r_l = 0, f"⚪ RSI {rsi:.0f}"
    return {"score": d_s + r_s, "max": 20, "label": f"{d_l}；{r_l}",
            "note": "底背離（價LL/指標HL）+ RSI 超賣", "sub": {"div_n": n, "rsi": (None if _nan(rsi) else float(rsi))}}


def _score_leverage_low(margin) -> dict:
    """槓桿清洗（max 20）= 融資餘額大減（散戶斷頭認賠＝底部訊號）。"""
    if not margin or margin.get("fin_chg_pct") is None:
        return {"score": 0, "max": 20, "label": "⚪ 無融資資料",
                "note": "融資大減＝散戶斷頭清洗〔未擬合〕", "sub": {}}
    chg = margin["fin_chg_pct"]
    if chg <= -5: s, l = 20, f"🟢 融資暴減 {chg:+.1f}%（斷頭清洗）"
    elif chg <= -3: s, l = 14, f"🟢 融資大減 {chg:+.1f}%"
    elif chg <= -1: s, l = 7, f"🟡 融資減 {chg:+.1f}%"
    else: s, l = 0, f"⚪ 融資 {chg:+.1f}%（未清洗）"
    return {"score": s, "max": 20, "label": l, "note": "融資餘額日變化（散戶槓桿清洗）",
            "sub": {"fin_chg_pct": chg}}


def _score_institution_low(institutional, df) -> dict:
    """法人吸籌（max 20）= 三大法人買超（以近 20 日均量正規化）。"""
    if not institutional or institutional.get("total_net") is None:
        return {"score": 0, "max": 20, "label": "⚪ 無法人資料",
                "note": "三大法人買超＝吸籌〔未擬合〕", "sub": {}}
    net = institutional["total_net"]
    av = _avg_vol(df)
    ratio = (net / av * 100) if av else None    # 買賣超占均量 %
    if ratio is None:
        s, l = (7, "🟡 法人買超（無量基準）") if net > 0 else (0, "⚪ 法人賣超/平")
    elif ratio >= 20: s, l = 20, f"🟢 法人大買 {ratio:+.0f}%均量"
    elif ratio >= 8: s, l = 13, f"🟢 法人買超 {ratio:+.0f}%均量"
    elif ratio >= 3: s, l = 6, f"🟡 法人小買 {ratio:+.0f}%均量"
    else: s, l = 0, f"⚪ 法人 {ratio:+.0f}%均量（賣/平）"
    return {"score": s, "max": 20, "label": l, "note": "三大法人買賣超 / 近20日均量",
            "sub": {"total_net": net, "ratio_pct": ratio}}


def _score_tdcc_low(tdcc) -> dict:
    """大戶吸籌（max 15）= TDCC 大戶（≥1000張）持股比高。"""
    if not tdcc or tdcc.get("major_pct") is None:
        return {"score": 0, "max": 15, "label": "⚪ 無集保資料",
                "note": "大戶持股比高＝籌碼集中吸籌〔未擬合〕", "sub": {}}
    mp = tdcc["major_pct"]
    if mp >= 70: s, l = 15, f"🟢 大戶 {mp:.0f}%（高度集中）"
    elif mp >= 55: s, l = 10, f"🟢 大戶 {mp:.0f}%（集中）"
    elif mp >= 40: s, l = 5, f"🟡 大戶 {mp:.0f}%"
    else: s, l = 0, f"⚪ 大戶 {mp:.0f}%（分散）"
    return {"score": s, "max": 15, "label": l, "note": "TDCC 大戶（≥1000張）持股比",
            "sub": {"major_pct": mp, "retail_pct": tdcc.get("retail_pct")}}


def compute_relative_low_tw(row, df=None, *, chip=None) -> Tuple[int, Dict[str, dict]]:
    """
    台股相對底部五維評分（0–100）。chip = service.tw_chip.get_chip_bundle 結果（可缺）。
    回傳 (score, signals)。
    """
    chip = chip or {}
    signals = {
        "valuation": _score_valuation_low(chip.get("valuation")),
        "technical": _score_technical_low(row, df),
        "leverage": _score_leverage_low(chip.get("margin")),
        "institution": _score_institution_low(chip.get("institutional"), df),
        "tdcc": _score_tdcc_low(chip.get("tdcc")),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_low_tw_meta(score: int) -> Tuple[str, str, str]:
    if score >= 65: return "🟢 強力低估", "#00cc88", "估值＋籌碼俱佳，分批進場（配合趨勢確認）"
    if score >= 45: return "🟢 明確低估", "#00aa66", "可開始定投/減空"
    if score >= 30: return "🟡 偏冷觀察", "#ffcc00", "留意打底，勿純憑超賣搶反彈"
    if score >= 15: return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🔴 無底部訊號", "#ff4b4b", "無低估壓力，勿接刀"
