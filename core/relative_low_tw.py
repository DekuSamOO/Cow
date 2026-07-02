"""
core/relative_low_tw.py  ·  v0.3（2026-07-02 通用量價/結構維度）
台股相對底部（抄底雷達）— 純函數、零網路請求。把加密專屬維度（funding/OI/鏈上）
替換為台股對應（融資融券/三大法人/TDCC/PE-PB）。

七維（原五維已回測校準維度總分 100 不動；新增兩維為**疊加**、不佔既有維度配額，
理論總分 110，`compute_relative_low_tw` 仍 clamp 到 100）：
  槓桿清洗 30（融資大減＝散戶斷頭）      ← ✅ 校準最強維（swing 真底vs假底 AUC 0.564）
  技術回穩 25（底背離 + RSI 超賣）       ← 複用 core/divergence（與加密同源、已驗證）
  法人吸籌 20（三大法人買超）           ← 〔弱〕swing AUC 0.542
  大戶吸籌 15（TDCC 大戶持股比高）       ← 〔弱・樣本薄 2023-09起〕swing AUC 0.423
  估值深跌 10（PE/PB 絕對值低）         ← 〔雜訊〕swing AUC 0.45（台股便宜≠反彈，價值陷阱）→ 大降權
  量價背離  6（量縮價增＝賣壓竭盡）      ← 🆕 v0.3 新增，`core.relative_universal`，規則式未回測；
                                          台股既有框架**原本沒有任何量能維度**（高側量能見頂 v0.3
                                          有，低側缺），這維補的正是這個缺口
  結構轉折  4（前低未破＝結構轉強）       ← 🆕 v0.3 新增，`core.relative_universal`，規則式未回測

校準關鍵（v0.1→v0.2）：槓桿 20→30（最強）、估值 25→10（雜訊，與加密「底部靠估值」相反）。
v0.2→v0.3：新增量價背離/結構轉折（`core.relative_universal`，通用軸、美股台股共用）。
**刻意不動既有五維的內部分級**（無真實回測數據支撐前，不臆測精確配重數字，違反本專案
「誠實與驗證」原則）→ 疊加式新增，總分變 110、clamp 100。待累積跨市場資料回測出 AUC 後，
比照既有方法論用實測數字重新按比例分配。
PE/PB 用絕對值（與逃頂一致）。〔弱/雜訊〕維度＝已回測 AUC<0.55、給低權、判讀僅參考。
台股底部與加密非對稱：加密底部靠長週期估值，台股底部靠「融資斷頭清洗」。
"""
from typing import Optional, Dict, Tuple

from core.divergence import detect_bottom_divergence_combo
from core.relative_universal import (score_volume_price_bottom, score_structure_bottom, rescale_dim,
                                     _nan, low_meta_ladder)

WEIGHTS_LOW_TW = {
    "leverage": 30, "technical": 25, "institution": 20, "tdcc": 15, "valuation": 10,
    "vol_price": 6, "structure": 4,
}
# AUC<0.55 的弱維（已回測、給低權、判讀僅參考）
WEAK_DIMS_LOW_TW = ("institution", "tdcc", "valuation")
# 從未回測（規則式，非「回測後發現弱」）— 與 WEAK_DIMS 狀態不同，別搞混。
UNFITTED_DIMS_LOW_TW = ("vol_price", "structure")


def _avg_vol(df) -> Optional[float]:
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < 5:
        return None
    v = float(df["volume"].tail(20).mean())
    return v if v > 0 else None


def _score_leverage_low(margin) -> dict:
    """槓桿清洗（max 30，校準最強維）= 融資餘額大減（散戶斷頭認賠＝底部訊號）。"""
    if not margin or margin.get("fin_chg_pct") is None:
        return {"score": 0, "max": 30, "label": "⚪ 無融資資料",
                "note": "融資大減＝散戶斷頭清洗（抄底最強維 AUC 0.564）", "sub": {}}
    chg = margin["fin_chg_pct"]
    if chg <= -5: s, l = 30, f"🟢 融資暴減 {chg:+.1f}%（斷頭清洗）"
    elif chg <= -3: s, l = 21, f"🟢 融資大減 {chg:+.1f}%"
    elif chg <= -1: s, l = 10, f"🟡 融資減 {chg:+.1f}%"
    else: s, l = 0, f"⚪ 融資 {chg:+.1f}%（未清洗）"
    return {"score": s, "max": 30, "label": l,
            "note": "融資餘額日變化（散戶槓桿清洗，swing 真底vs假底 AUC 0.564）",
            "sub": {"fin_chg_pct": chg}}


def _score_technical_low(row, df) -> dict:
    """技術回穩（max 25）= 底背離(17) + RSI_14 超賣(8)。複用既有 divergence（通用）。"""
    div = detect_bottom_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    if n >= 2: d_s, d_l = 17, "🟢 RSI+MACD 雙底背離"
    elif n == 1: d_s, d_l = round(7 + 5 * div.get("strength", 0.0)), "🟡 單指標底背離"
    else: d_s, d_l = 0, "⚪ 無底背離"
    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi): r_s, r_l = 0, "⚪ RSI 無"
    elif rsi <= 20: r_s, r_l = 8, f"🟢 RSI {rsi:.0f} 極度超賣"
    elif rsi <= 30: r_s, r_l = 4, f"🟡 RSI {rsi:.0f} 超賣"
    else: r_s, r_l = 0, f"⚪ RSI {rsi:.0f}"
    return {"score": d_s + r_s, "max": 25, "label": f"{d_l}；{r_l}",
            "note": "底背離（價LL/指標HL）+ RSI 超賣", "sub": {"div_n": n, "rsi": (None if _nan(rsi) else float(rsi))}}


def _score_institution_low(institutional, df) -> dict:
    """法人吸籌（max 20，〔弱〕）= 三大法人買超（以近 20 日均量正規化）。"""
    if not institutional or institutional.get("total_net") is None:
        return {"score": 0, "max": 20, "label": "⚪ 無法人資料",
                "note": "三大法人買超〔弱 AUC 0.542〕", "sub": {}}
    net = institutional["total_net"]
    av = _avg_vol(df)
    ratio = (net / av * 100) if av else None
    if ratio is None:
        s, l = (7, "🟡 法人買超（無量基準）") if net > 0 else (0, "⚪ 法人賣超/平")
    elif ratio >= 20: s, l = 20, f"🟢 法人大買 {ratio:+.0f}%均量"
    elif ratio >= 8: s, l = 13, f"🟢 法人買超 {ratio:+.0f}%均量"
    elif ratio >= 3: s, l = 6, f"🟡 法人小買 {ratio:+.0f}%均量"
    else: s, l = 0, f"⚪ 法人 {ratio:+.0f}%均量（賣/平）"
    return {"score": s, "max": 20, "label": l, "note": "三大法人買賣超/均量〔弱 AUC 0.542〕",
            "sub": {"total_net": net, "ratio_pct": ratio}}


def _score_tdcc_low(tdcc) -> dict:
    """大戶吸籌（max 15，〔弱・樣本薄〕）= TDCC 大戶（≥1000張）持股比高。"""
    if not tdcc or tdcc.get("major_pct") is None:
        return {"score": 0, "max": 15, "label": "⚪ 無集保資料",
                "note": "大戶持股比高〔弱 AUC 0.423・樣本薄〕", "sub": {}}
    mp = tdcc["major_pct"]
    if mp >= 70: s, l = 15, f"🟢 大戶 {mp:.0f}%（高度集中）"
    elif mp >= 55: s, l = 10, f"🟢 大戶 {mp:.0f}%（集中）"
    elif mp >= 40: s, l = 5, f"🟡 大戶 {mp:.0f}%"
    else: s, l = 0, f"⚪ 大戶 {mp:.0f}%（分散）"
    return {"score": s, "max": 15, "label": l, "note": "TDCC 大戶（≥1000張）持股比〔弱・樣本薄〕",
            "sub": {"major_pct": mp, "retail_pct": tdcc.get("retail_pct")}}


def _score_valuation_low(valuation) -> dict:
    """估值深跌（max 10，〔雜訊〕降權）= PE 低(5) + PB 低(5)。台股便宜≠反彈（價值陷阱）。"""
    if not valuation:
        return {"score": 0, "max": 10, "label": "⚪ 無估值資料（上櫃/缺）",
                "note": "PE/PB 絕對值〔雜訊 AUC 0.45，台股價值陷阱→大降權〕", "sub": {}}
    pe, pb = valuation.get("pe"), valuation.get("pb")
    if _nan(pe) or pe <= 0: pe_s, pe_l = 0, "⚪ PE 無/負"
    elif pe < 10: pe_s, pe_l = 5, f"🟢 PE {pe:.0f} 低估(<10)"
    elif pe < 15: pe_s, pe_l = 3, f"🟡 PE {pe:.0f} 偏低(<15)"
    else: pe_s, pe_l = 0, f"⚪ PE {pe:.0f}"
    if _nan(pb) or pb <= 0: pb_s, pb_l = 0, "⚪ PB 無"
    elif pb < 1.0: pb_s, pb_l = 5, f"🟢 PB {pb:.2f} 破淨(<1)"
    elif pb < 1.5: pb_s, pb_l = 3, f"🟡 PB {pb:.2f} 偏低(<1.5)"
    else: pb_s, pb_l = 0, f"⚪ PB {pb:.2f}"
    return {"score": pe_s + pb_s, "max": 10, "label": f"{pe_l}；{pb_l}",
            "note": "PE/PB 絕對值深跌〔雜訊 AUC 0.45・價值陷阱，已大降權〕",
            "sub": {"pe": pe, "pb": pb, "pe_score": pe_s, "pb_score": pb_s}}


def compute_relative_low_tw(row, df=None, *, chip=None) -> Tuple[int, Dict[str, dict]]:
    """台股相對底部七維評分（0–100，clamp）。chip = service.tw_chip.get_chip_bundle 結果（可缺）。"""
    chip = chip or {}
    signals = {
        "leverage": _score_leverage_low(chip.get("margin")),
        "technical": _score_technical_low(row, df),
        "institution": _score_institution_low(chip.get("institutional"), df),
        "tdcc": _score_tdcc_low(chip.get("tdcc")),
        "valuation": _score_valuation_low(chip.get("valuation")),
        "vol_price": rescale_dim(score_volume_price_bottom(df), WEIGHTS_LOW_TW["vol_price"]),
        "structure": rescale_dim(score_structure_bottom(df), WEIGHTS_LOW_TW["structure"]),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_low_tw_meta(score: int) -> Tuple[str, str, str]:
    return low_meta_ladder(score, "槓桿清洗＋技術回穩俱佳，分批進場（配合趨勢確認）")
