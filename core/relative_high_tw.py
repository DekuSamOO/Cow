"""
core/relative_high_tw.py  ·  v0.3（2026-06-26 弱維強化）
台股相對高點（逃頂雷達）— 純函數、零網路請求。把加密專屬維度（funding/OI/鏈上）
替換為台股對應（融資融券/三大法人/TDCC/PE-PB/成交量）。

六維（0–100，權重經 swing-only 回測校準，見 scripts/tw_swing_backtest.py / tw_variant_backtest.py）：
  技術衰竭 30（頂背離 + RSI 超買）        ← 複用 core/divergence（與加密同源、已驗證）
  估值過高 30（PE/PB 絕對值高）          ← ✅ 最強維（swing 逃頂 AUC PE 0.626/PB 0.640）
  量能見頂 18（成交量處個股自身高分位）    ← ✅ v0.3 新增（swing 逃頂 AUC 0.648，跨 labeling 穩健、抄底側中性）
  槓桿過熱 10（融資增速高＝散戶追高）      ← 〔弱〕swing AUC 0.538
  法人派發  4（三大法人賣超）            ← 〔弱/雜訊〕swing AUC 0.519 → 降權
  籌碼鬆動  8（TDCC 散戶持股比高）       ← 〔弱・樣本薄 2023-09起〕swing AUC 0.538

校準關鍵：
  v0.1→v0.2：估值 15→30（最強）、法人 25→10（雜訊）。
  v0.2→v0.3：新增量能見頂 18（自身成交量分位，自包含、不依賴 climber）；
            為配重把融資 15→10、法人 10→4、TDCC 15→8（皆弱維）。
PE/PB **用絕對值非分位**（swing 重測：絕對 PE 0.626 大勝個股分位 0.452/0.452，且分位多次反向 <0.5）。
量能維用**個股自身分位**（爆量見頂）非絕對量（跨股不可比）；P4 驗證其逃頂 0.61–0.67、抄底 0.49–0.51 不對稱
→ 非「會大動」的移動幅度混淆（券資比/波動率分位即因雙向皆 >0.55 被否決）。
〔弱〕維度＝有回測但 AUC<0.55、給低權；技術維度沿用加密側既有驗證。
"""
import math
from typing import Optional, Dict, Tuple

from core.divergence import detect_top_divergence_combo

WEIGHTS_HIGH_TW = {
    "technical": 30, "valuation": 30, "volume": 18, "leverage": 10, "institution": 4, "tdcc": 8,
}
# AUC<0.55 的弱維（已回測、給低權、判讀僅參考）。volume/valuation/technical 非弱維。
WEAK_DIMS_HIGH_TW = ("leverage", "institution", "tdcc")


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _avg_vol(df) -> Optional[float]:
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < 5:
        return None
    v = float(df["volume"].tail(20).mean())
    return v if v > 0 else None


def _vol_pctile(df) -> Optional[float]:
    """最新成交量在個股自身歷史的分位（0–1，midrank 處理 ties）。
    自包含、零外部依賴（用 df 本身的 volume 歷史）→ 與 watcher live 自包含原則一致。
    回測對應 scripts 的 per_stock_pctile（成交量 expanding 分位）。"""
    if df is None or "volume" not in getattr(df, "columns", []) or len(df) < 60:
        return None
    v = df["volume"].dropna()
    if len(v) < 60:
        return None
    latest = float(v.iloc[-1])
    if latest <= 0:
        return None
    arr = v.to_numpy(dtype=float)
    # midrank：定值序列回 0.5（非「爆量」），避免常數 volume 誤判滿分
    return float(((arr < latest).sum() + 0.5 * (arr == latest).sum()) / len(arr))


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


def _score_valuation_high(valuation) -> dict:
    """估值過高（max 30，校準最強維）= PE 高(16) + PB 高(14)。絕對值分級。"""
    if not valuation:
        return {"score": 0, "max": 30, "label": "⚪ 無估值資料（上櫃/缺）",
                "note": "PE/PB 絕對值（逃頂最強維 AUC~0.63）", "sub": {}}
    pe, pb = valuation.get("pe"), valuation.get("pb")
    if _nan(pe) or pe <= 0: pe_s, pe_l = 0, "⚪ PE 無/負"
    elif pe >= 40: pe_s, pe_l = 16, f"🔴 PE {pe:.0f} 偏貴(≥40)"
    elif pe >= 25: pe_s, pe_l = 9, f"🟠 PE {pe:.0f} 偏高(≥25)"
    elif pe >= 18: pe_s, pe_l = 4, f"🟡 PE {pe:.0f} 略高(≥18)"
    else: pe_s, pe_l = 0, f"⚪ PE {pe:.0f}"
    if _nan(pb) or pb <= 0: pb_s, pb_l = 0, "⚪ PB 無"
    elif pb >= 5: pb_s, pb_l = 14, f"🔴 PB {pb:.1f} 偏貴(≥5)"
    elif pb >= 3: pb_s, pb_l = 8, f"🟠 PB {pb:.1f} 偏高(≥3)"
    elif pb >= 2: pb_s, pb_l = 3, f"🟡 PB {pb:.1f} 略高(≥2)"
    else: pb_s, pb_l = 0, f"⚪ PB {pb:.1f}"
    return {"score": pe_s + pb_s, "max": 30, "label": f"{pe_l}；{pb_l}",
            "note": "PE/PB 絕對值過高（swing 逃頂 AUC PE 0.627/PB 0.640，絕對勝分位）",
            "sub": {"pe": pe, "pb": pb}}


def _score_volume_high(row, df) -> dict:
    """量能見頂（max 18，v0.3 新增最強新維）= 成交量處個股自身高分位（爆量見頂）。
    swing 逃頂 AUC 0.648、跨 labeling 0.61–0.67、抄底側 0.49–0.51（不對稱，非移動幅度混淆）。"""
    pct = _vol_pctile(df)
    if pct is None:
        return {"score": 0, "max": 18, "label": "⚪ 量能資料不足",
                "note": "成交量個股自身分位（爆量見頂，AUC 0.648）", "sub": {}}
    if pct >= 0.95: s, l = 18, f"🔴 爆量 {pct*100:.0f}分位（量能見頂）"
    elif pct >= 0.85: s, l = 12, f"🟠 量增 {pct*100:.0f}分位"
    elif pct >= 0.70: s, l = 6, f"🟡 量偏高 {pct*100:.0f}分位"
    else: s, l = 0, f"⚪ 量能 {pct*100:.0f}分位（正常）"
    return {"score": s, "max": 18, "label": l, "note": "成交量個股自身分位（爆量見頂，swing AUC 0.648）",
            "sub": {"vol_pctile": pct}}


def _score_leverage_high(margin) -> dict:
    """槓桿過熱（max 10，〔弱〕）= 融資餘額增速高（散戶追高加槓桿）。"""
    if not margin or margin.get("fin_chg_pct") is None:
        return {"score": 0, "max": 10, "label": "⚪ 無融資資料",
                "note": "融資增速高＝散戶追高〔弱 AUC 0.538〕", "sub": {}}
    chg = margin["fin_chg_pct"]
    if chg >= 5: s, l = 10, f"🔴 融資暴增 {chg:+.1f}%（散戶追高）"
    elif chg >= 3: s, l = 7, f"🟠 融資大增 {chg:+.1f}%"
    elif chg >= 1: s, l = 3, f"🟡 融資增 {chg:+.1f}%"
    else: s, l = 0, f"⚪ 融資 {chg:+.1f}%（未過熱）"
    return {"score": s, "max": 10, "label": l, "note": "融資餘額日變化〔弱 AUC 0.538〕",
            "sub": {"fin_chg_pct": chg}}


def _score_institution_high(institutional, df) -> dict:
    """法人派發（max 4，〔弱/雜訊〕）= 三大法人賣超（近20日均量正規化）。"""
    if not institutional or institutional.get("total_net") is None:
        return {"score": 0, "max": 4, "label": "⚪ 無法人資料",
                "note": "三大法人賣超〔弱 AUC 0.519，降權〕", "sub": {}}
    net = institutional["total_net"]
    av = _avg_vol(df)
    ratio = (net / av * 100) if av else None
    if ratio is None:
        s, l = (2, "🟠 法人賣超（無量基準）") if net < 0 else (0, "⚪ 法人買超/平")
    elif ratio <= -20: s, l = 4, f"🟠 法人大賣 {ratio:+.0f}%均量"
    elif ratio <= -8: s, l = 2, f"🟡 法人賣超 {ratio:+.0f}%均量"
    elif ratio <= -3: s, l = 1, f"🟡 法人小賣 {ratio:+.0f}%均量"
    else: s, l = 0, f"⚪ 法人 {ratio:+.0f}%均量（買/平）"
    return {"score": s, "max": 4, "label": l, "note": "三大法人買賣超/均量〔弱 AUC 0.519〕",
            "sub": {"total_net": net, "ratio_pct": ratio}}


def _score_tdcc_high(tdcc) -> dict:
    """籌碼鬆動（max 8，〔弱・樣本薄〕）= TDCC 散戶持股比高（籌碼分散＝派發末端）。"""
    if not tdcc or tdcc.get("retail_pct") is None:
        return {"score": 0, "max": 8, "label": "⚪ 無集保資料",
                "note": "散戶持股比高〔弱 AUC 0.538・樣本薄〕", "sub": {}}
    rp = tdcc["retail_pct"]
    if rp >= 40: s, l = 8, f"🔴 散戶 {rp:.0f}%（籌碼鬆散）"
    elif rp >= 30: s, l = 5, f"🟠 散戶 {rp:.0f}%"
    elif rp >= 25: s, l = 2, f"🟡 散戶 {rp:.0f}%"
    else: s, l = 0, f"⚪ 散戶 {rp:.0f}%（集中）"
    return {"score": s, "max": 8, "label": l, "note": "TDCC 散戶（≤50張）持股比〔弱・樣本薄〕",
            "sub": {"retail_pct": rp, "major_pct": tdcc.get("major_pct")}}


def compute_relative_high_tw(row, df=None, *, chip=None) -> Tuple[int, Dict[str, dict]]:
    """台股相對高點五維評分（0–100）。chip = service.tw_chip.get_chip_bundle 結果（可缺）。"""
    chip = chip or {}
    signals = {
        "technical": _score_technical_high(row, df),
        "valuation": _score_valuation_high(chip.get("valuation")),
        "volume": _score_volume_high(row, df),
        "leverage": _score_leverage_high(chip.get("margin")),
        "institution": _score_institution_high(chip.get("institutional"), df),
        "tdcc": _score_tdcc_high(chip.get("tdcc")),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_high_tw_meta(score: int) -> Tuple[str, str, str]:
    if score >= 65: return "🔴 強烈逃頂", "#ff4b4b", "技術＋估值俱過熱，分批止盈/減碼"
    if score >= 45: return "🟠 明確過熱", "#ff8800", "減碼、收緊移動止盈"
    if score >= 30: return "🟡 偏熱警戒", "#ffcc00", "停止加倉、提高警覺"
    if score >= 15: return "⚪ 中性", "#9e9e9e", "正常持有"
    return "🟢 無過熱", "#00cc88", "無逃頂壓力"
