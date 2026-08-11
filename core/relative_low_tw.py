"""
core/relative_low_tw.py  ·  v0.4（2026-07-02 疊加新維回測拍板 + 反指標維移除）
台股相對底部（抄底雷達）— 純函數、零網路請求。把加密專屬維度（funding/OI/鏈上）
替換為台股對應（融資融券/三大法人/PE-PB）。

四維（總分 100）：
  槓桿清洗 40（融資大減＝散戶斷頭）      ← ✅ 校準最強維（swing 真底vs假底 AUC 0.564）；v0.4 提權 30→40
  技術回穩 30（底背離 + RSI 超賣）       ← 複用 core/divergence（與加密同源、已驗證）；v0.4 提權 25→30
  法人吸籌 20（三大法人買超）           ← 〔弱〕swing AUC 0.542
  估值深跌 10（PE/PB 絕對值低）         ← 〔雜訊〕swing AUC 0.45（台股便宜≠反彈，價值陷阱）→ 大降權

校準關鍵：
  v0.1→v0.2：槓桿 20→30（最強）、估值 25→10（雜訊，與加密「底部靠估值」相反）。
  v0.2→v0.3：疊加量價背離/結構轉折（`core.relative_universal`），先標未擬合。
  v0.3→v0.4：**2026-07-02 全市場 swing 回測拍板**（`scripts/tw_universal_backtest.py`，
            2080 檔、out-of-sample≥2024）三項處置：
              (1) **大戶吸籌（TDCC major_pct）AUC 0.422（方向反、比亂猜差）→ 整維移除**
                  （原 15 分、本就〔弱・樣本薄〕；移除同 Hash Ribbons 邏輯，不留反指標計分）。
              (2) 量價背離抄底 AUC **0.500（純雜訊）**、結構轉折抄底 **0.516（弱雜訊）→ 皆移除**
                  （「量能維補抄底缺口」的嘗試被資料否決；純函數仍在 relative_universal 供美股用）。
              (3) 釋出的 15 分**重配給最強兩維**：槓桿清洗 30→40（實測最強 0.564）、技術回穩 25→30；
                  institution/valuation 配額不動。四維重回總分 100（用 rescale_dim 提權、不動內部分級）。
PE/PB 用絕對值（與逃頂一致）。〔弱/雜訊〕維度＝已回測 AUC<0.55、給低權、判讀僅參考。
台股底部與加密非對稱：加密底部靠長週期估值，台股底部靠「融資斷頭清洗」。
"""
from typing import Dict, Tuple

from core.divergence import detect_bottom_divergence_combo
from core.relative_universal import rescale_dim, _nan, low_meta_ladder, avg_vol as _avg_vol

WEIGHTS_LOW_TW = {
    "leverage": 40, "technical": 30, "institution": 20, "valuation": 10,
}
# AUC<0.55 的弱維（已回測、給低權、判讀僅參考）
WEAK_DIMS_LOW_TW = ("institution", "valuation")
# 已無「未擬合」維：vol_price/structure 抄底 2026-07-02 回測皆近雜訊(0.500/0.516) 已移除；
# tdcc 大戶抄底 AUC 0.422（方向反、比亂猜差）已移除，其 15 分權重重配給最強兩維（見 docstring）。
UNFITTED_DIMS_LOW_TW = ()


def _score_leverage_low(margin) -> dict:
    """槓桿清洗（max 30，校準最強維）= 融資餘額大減（散戶斷頭認賠＝底部訊號）。"""
    if not margin or margin.get("fin_chg_pct") is None:
        return {"score": 0, "max": 30, "label": "融資 ⚪ 無資料",
                "note": "融資大減＝散戶斷頭清洗（抄底最強維 AUC 0.564）", "sub": {}}
    chg = margin["fin_chg_pct"]
    if chg <= -5: s, l = 30, f"融資 🟢 暴減 {chg:+.1f}%（斷頭清洗）"
    elif chg <= -3: s, l = 21, f"融資 🟢 大減 {chg:+.1f}%"
    elif chg <= -1: s, l = 10, f"融資 🟡 減 {chg:+.1f}%"
    else: s, l = 0, f"融資 ⚪ {chg:+.1f}%（未清洗）"
    return {"score": s, "max": 30, "label": l,
            "note": "融資餘額日變化（散戶槓桿清洗，swing 真底vs假底 AUC 0.564）",
            "sub": {"fin_chg_pct": chg}}


def _score_technical_low(row, df) -> dict:
    """技術回穩（max 25）= 底背離(17) + RSI_14 超賣(8)。複用既有 divergence（通用）。"""
    div = detect_bottom_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    if n >= 2: d_s, d_l = 17, "🟢 RSI+MACD 雙底"
    elif n == 1: d_s, d_l = round(7 + 5 * div.get("strength", 0.0)), "🟡 單指標底"
    else: d_s, d_l = 0, "⚪ 無"
    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi): r_s, r_l = 0, "⚪ 無資料"
    elif rsi <= 20: r_s, r_l = 8, f"🟢 極度超賣({rsi:.0f})"
    elif rsi <= 30: r_s, r_l = 4, f"🟡 超賣({rsi:.0f})"
    else: r_s, r_l = 0, f"⚪ 中性({rsi:.0f})"
    return {"score": d_s + r_s, "max": 25, "label": f"背離 {d_l}；RSI {r_l}",
            "note": "底背離（價LL/指標HL）+ RSI 超賣", "sub": {"div_n": n, "rsi": (None if _nan(rsi) else float(rsi))}}


def _score_institution_low(institutional, df) -> dict:
    """法人吸籌（max 20，〔弱〕）= 三大法人買超（以近 20 日均量正規化）。"""
    if not institutional or institutional.get("total_net") is None:
        return {"score": 0, "max": 20, "label": "法人 ⚪ 無資料",
                "note": "三大法人買超〔弱 AUC 0.542〕", "sub": {}}
    net = institutional["total_net"]
    av = _avg_vol(df)
    ratio = (net / av * 100) if av else None
    if ratio is None:
        s, l = (7, "法人 🟡 買超（無量基準）") if net > 0 else (0, "法人 ⚪ 賣超/平")
    elif ratio >= 20: s, l = 20, f"法人 🟢 大買 {ratio:+.0f}%均量"
    elif ratio >= 8: s, l = 13, f"法人 🟢 買超 {ratio:+.0f}%均量"
    elif ratio >= 3: s, l = 6, f"法人 🟡 小買 {ratio:+.0f}%均量"
    else: s, l = 0, f"法人 ⚪ {ratio:+.0f}%均量（賣/平）"
    return {"score": s, "max": 20, "label": l, "note": "三大法人買賣超/均量〔弱 AUC 0.542〕",
            "sub": {"total_net": net, "ratio_pct": ratio}}


def _score_valuation_low(valuation) -> dict:
    """估值深跌（max 10，〔雜訊〕降權）= PE 低(5) + PB 低(5)。台股便宜≠反彈（價值陷阱）。"""
    if not valuation:
        return {"score": 0, "max": 10, "label": "估值 ⚪ 無資料（上櫃/缺）",
                "note": "PE/PB 絕對值〔雜訊 AUC 0.45，台股價值陷阱→大降權〕", "sub": {}}
    pe, pb = valuation.get("pe"), valuation.get("pb")
    if _nan(pe) or pe <= 0: pe_s, pe_l = 0, "⚪ 無/負"
    elif pe < 10: pe_s, pe_l = 5, f"🟢 {pe:.0f} 低估(<10)"
    elif pe < 15: pe_s, pe_l = 3, f"🟡 {pe:.0f} 偏低(<15)"
    else: pe_s, pe_l = 0, f"⚪ {pe:.0f}"
    if _nan(pb) or pb <= 0: pb_s, pb_l = 0, "⚪ 無"
    elif pb < 1.0: pb_s, pb_l = 5, f"🟢 {pb:.2f} 破淨(<1)"
    elif pb < 1.5: pb_s, pb_l = 3, f"🟡 {pb:.2f} 偏低(<1.5)"
    else: pb_s, pb_l = 0, f"⚪ {pb:.2f}"
    return {"score": pe_s + pb_s, "max": 10, "label": f"PE {pe_l}；PB {pb_l}",
            "note": "PE/PB 絕對值深跌〔雜訊 AUC 0.45・價值陷阱，已大降權〕",
            "sub": {"pe": pe, "pb": pb, "pe_score": pe_s, "pb_score": pb_s}}


def compute_relative_low_tw(row, df=None, *, chip=None,
                            forming_last: bool = False) -> Tuple[int, Dict[str, dict]]:
    """台股相對底部四維評分（0–100，clamp）。chip = service.tw_chip.get_chip_bundle 結果（可缺）。

    forming_last=True：df 最後一根是「今日進行式」日棒（live 盤中）→ **法人吸籌的均量分母**
    改以已結算日為準（抄底側只有這一維吃量，逃頂側的對應說明見 `compute_relative_high_tw`）。
    不排除的話 20 日均量分母混進「只累積到當下的今日量」又擠掉一根已結算日，早盤分母偏小
    ~4.9%：實測 2022 年起 176.7 萬股票日，**買超日有 2.48% 整格跳動（平均 6.8 分）**，方向
    近乎單向高估（早盤高估:低估 = 342:1），其中 4,358 筆從 0 分跳成有分。抄底分數被高估＝
    更早喊「可以接」，是最危險的方向，且 6.8 分跨得過 65/45/30/15 階梯與 composite 的
    LOW_VALUE 門檻。（2026-08-11 實測推翻「偏差跨不過門檻」的舊否決。）
    價格類維度（技術回穩）仍用進行式那根＝當下價，語意本就該即時；槓桿/估值不吃 df。
    回測/校準腳本餵的是 EOD 日線，維持預設 False（PiT 口徑不變）。"""
    chip = chip or {}
    df_settled = df.iloc[:-1] if (forming_last and df is not None and len(df) >= 2) else df
    # leverage/technical 用 rescale_dim 提權（融資清洗 30→40、技術回穩 25→30，吸收 tdcc 移除的 15
    # 分），不動 _score_* 內部分級；institution/valuation 維持原配額。四維總分 100。
    signals = {
        "leverage": rescale_dim(_score_leverage_low(chip.get("margin")), WEIGHTS_LOW_TW["leverage"]),
        "technical": rescale_dim(_score_technical_low(row, df), WEIGHTS_LOW_TW["technical"]),
        "institution": _score_institution_low(chip.get("institutional"), df_settled),
        "valuation": _score_valuation_low(chip.get("valuation")),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_low_tw_meta(score: int) -> Tuple[str, str, str]:
    return low_meta_ladder(score, "槓桿清洗＋技術回穩俱佳，分批進場（配合趨勢確認）")
