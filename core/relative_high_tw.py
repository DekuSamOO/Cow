"""
core/relative_high_tw.py  ·  v0.5（2026-07-02 疊加新維回測拍板）
台股相對高點（逃頂雷達）— 純函數、零網路請求。把加密專屬維度（funding/OI/鏈上）
替換為台股對應（融資融券/三大法人/TDCC/PE-PB/成交量）。

七維（六維已校準核心總分 100 不動；vol_price 為**已驗證疊加**、理論總分 108，
`compute_relative_high_tw` 仍 clamp 到 100）：
  技術衰竭 30（頂背離 + RSI 超買）        ← 複用 core/divergence（與加密同源、已驗證）
  估值過高 30（PE/PB 絕對值高）          ← ✅ 最強維（swing 逃頂 AUC PE 0.626/PB 0.640）
  量能見頂 18（近 5 日均量處個股自身高分位）← ✅ v0.3 新增（swing 逃頂 AUC 0.648，跨 labeling 穩健、抄底側中性）
  槓桿過熱 10（融資增速高＝散戶追高）      ← 〔弱〕swing AUC 0.538
  法人派發  4（三大法人賣超）            ← 〔弱/雜訊〕swing AUC 0.519 → 降權
  籌碼鬆動  8（TDCC 散戶持股比高）       ← 〔弱・樣本薄 2023-09起〕swing AUC 0.538
  量價背離  8（量增價縮＝出貨）          ← ✅ v0.5 轉正式（swing 逃頂 AUC 0.566，2026-07-02 全市場回測>0.55）

校準關鍵：
  v0.1→v0.2：估值 15→30（最強）、法人 25→10（雜訊）。
  v0.2→v0.3：新增量能見頂 18（自身成交量分位，自包含、不依賴 climber）；
            為配重把融資 15→10、法人 10→4、TDCC 15→8（皆弱維）。
  v0.3→v0.4：新增量價背離/結構轉折（`core.relative_universal`，通用軸），先疊加、標未擬合。
  v0.4→v0.5：**2026-07-02 全市場 swing 回測拍板**（`scripts/tw_universal_backtest.py`，
            2080 檔、out-of-sample≥2024）：量價背離逃頂 **AUC 0.566>0.55** → 轉正式權重
            5→8（與弱維 leverage/散戶 同級疊加）；結構轉折逃頂 **AUC 0.483（雜訊/微反）→ 移除**
            （不再計分；純函數仍在 relative_universal 供美股框架用）。核心六維內部分級不動。
  v0.5→v0.5.1：**2026-08-10 均量視窗校準**（`scripts/tw_volwindow_calib.py`）量能維由單日量
            改吃 `VOL_WINDOW`=5 日均量（判別力等價、判讀更穩，見 `vol_pctile`）；並新增
            `forming_last` 供 live 盤中排除未結算今日棒。**配重與分級門檻不動。**
PE/PB **用絕對值非分位**（swing 重測：絕對 PE 0.626 大勝個股分位 0.452/0.452，且分位多次反向 <0.5）。
量能維用**個股自身分位**（爆量見頂）非絕對量（跨股不可比）；P4 驗證其逃頂 0.61–0.67、抄底 0.49–0.51 不對稱
→ 非「會大動」的移動幅度混淆（券資比/波動率分位即因雙向皆 >0.55 被否決）。
〔弱〕維度＝有回測但 AUC<0.55、給低權；技術維度沿用加密側既有驗證。
"""
from typing import Optional, Dict, Tuple

from core.divergence import detect_top_divergence_combo
from core.relative_universal import (score_volume_price_top, rescale_dim,
                                     _nan, high_meta_ladder, avg_vol as _avg_vol)

WEIGHTS_HIGH_TW = {
    "technical": 30, "valuation": 30, "volume": 18, "leverage": 10, "institution": 4, "tdcc": 8,
    "vol_price": 8,
}
# AUC<0.55 的弱維（已回測、給低權、判讀僅參考）。volume/valuation/technical 非弱維。
WEAK_DIMS_HIGH_TW = ("leverage", "institution", "tdcc")
# 已無「未擬合」維：vol_price 逃頂 2026-07-02 回測 AUC 0.566（>0.55）轉正式權重（見 docstring）；
# structure 逃頂 AUC 0.483（雜訊/微反）已移除，不再計分。
UNFITTED_DIMS_HIGH_TW = ()
# 量能維的均量視窗（交易日）；改此值＝改維度定義，須重跑 scripts/tw_volwindow_calib.py
VOL_WINDOW = 5
_VOL_NOTE = f"{VOL_WINDOW}日均量個股自身分位（爆量見頂，swing AUC 0.648）"


def _vol_series(df, *, drop_last: bool = False):
    """量能母體用的成交量序列（單一清洗出口，`vol_pctile` 與 `vol_snapshot` 共用）。
    **先剔最後一根、再 dropna**，順序不可顛倒：`fetch_ohlc` 會把 Yahoo 的幽靈零量列轉成
    NaN，若先 dropna 而最後一根恰為 NaN，它已被拿掉，`drop_last` 會再多砍一根已結算日。
    回傳 None 代表沒有 volume 欄（呼叫端一律當「資料不足」處理）。"""
    if df is None or "volume" not in getattr(df, "columns", []):
        return None
    v = df["volume"]
    if drop_last:
        v = v.iloc[:-1]
    return v.dropna()


def vol_pctile(df, *, window: int = VOL_WINDOW, drop_last: bool = False) -> Optional[float]:
    """近 `window` 日均量在個股自身歷史的分位（0–1，midrank 處理 ties）。
    自包含、零外部依賴（用 df 本身的 volume 歷史）→ 與 watcher live 自包含原則一致。
    回測對應 scripts 的 per_stock_pctile（expanding 分位，無 look-ahead）——本式「最新值對
    整段歷史排名」在最後一根上即 expanding 分位的當日值，故**只要 df 的歷史長度與校準面板
    對齊，兩邊就同口徑**（`service.ohlc_universal.fetch_ohlc` 預設 `rng="10y"` 即為此，
    改短會系統性推高分位，見該函式警語）。
    比較母體是 df 歷史上**每一天的同口徑 N 日均量**，故這是「量能水準的歷史排名」，
    **不是**「今日量 ÷ 近 N 日均量」那種量比倍數——兩者不可互相驗算（今日縮量與 N 日均量
    仍在歷史高檔可同時成立）。量比另見 `vol_snapshot` 的 `ratio`。

    window=5（2026-08-10 `scripts/tw_volwindow_calib.py` 拍板，2079 檔 / 446 萬列 / OOS≥2024）：
    swing 逃頂 AUC 1日 0.648、3日 0.650、5日 0.648、10日 0.646、20日 0.638，抄底側全 ≤0.521
    （無雙向混淆）。**5 日不是因為更準**（與現役單日判別力相同、差異在雜訊內），而是單日量
    一根爆量就跳滿格、隔天縮回又掉光，判讀不穩；10/20 日開始稀釋掉見頂訊號。不取名目最高的
    3 日：+0.002 是雜訊，挑峰值即過擬合。

    drop_last=True：整根排除最後一根（連比較母體一起排除），均量改由前 `window` 個已結算日算。
    供 live 呼叫端在**盤中**使用：Yahoo 日線最後一根在交易時段中是「今日進行式」，量只累積
    到當下，混進均量會系統性偏低（早盤尤甚），而回測餵的一律是已結算 EOD 日量 → 盤中不排除
    就等於 live 與回測口徑不一致。代價是量能維盤中整天不動，與台股籌碼/估值本就對齊 EOD
    最後交易日的行為一致。"""
    v = _vol_series(df, drop_last=drop_last)
    if v is None:
        return None
    if window > 1:
        v = v.rolling(window).mean().dropna()   # 母體＝歷史每一天的 N 日均量（同口徑才可比）
    if len(v) < 60:
        return None
    latest = float(v.iloc[-1])
    if latest <= 0:
        return None
    arr = v.to_numpy(dtype=float)
    # midrank：定值序列回 0.5（非「爆量」），避免常數 volume 誤判滿分
    return float(((arr < latest).sum() + 0.5 * (arr == latest).sum()) / len(arr))


def vol_snapshot(df, *, window: int = VOL_WINDOW, ref_window: int = 60,
                 drop_last: bool = False) -> Optional[dict]:
    """量能顯示用快照（**純顯示，不參與計分**）——與 `vol_pctile` 共用 `_vol_series`，
    畫面數字與計分分位不會漂移。回傳
    `{ma, pctile, ref_ma, ratio, ref_window, n_pop}`，資料不足回 None。

    存在理由：`pctile` 是「近 N 日均量在歷史的排名」，使用者卻自然會用「今日量 ÷ 近 N 日
    均量」去驗算它（2026-08-11 實際回報：219,571 ÷ 648,800 = 0.34「應該是 33 分位」）。
    兩者分母不同、不可互推，光改標籤文字擋不住誤讀（README v3.35 已修過一次仍再發生）→
    改成把量比 `ratio` 一起算出來給畫面並列，讓「今天縮量」與「5 日均量仍在歷史高檔」
    兩件事各自有數字，不必互相套用。
    `ratio` = 近 `window` 日均量 ÷ 近 `ref_window` 日均量；`ref_window=60` 是判讀慣例，
    **非校準值**（量能維計分只吃 `pctile`），改它不影響任何分數。"""
    v = _vol_series(df, drop_last=drop_last)
    if v is None or len(v) < ref_window:
        return None
    pct = vol_pctile(df, window=window, drop_last=drop_last)
    if pct is None:
        return None
    ma, ref_ma = float(v.tail(window).mean()), float(v.tail(ref_window).mean())
    if ma <= 0 or ref_ma <= 0:
        return None
    return {"ma": ma, "pctile": pct, "ref_ma": ref_ma, "ratio": ma / ref_ma,
            "ref_window": ref_window, "n_pop": len(v) - window + 1}


# W-7（2026-07-06）：升公開名 vol_pctile（watcher.py 曾直接 import 跨模組私名 _vol_pctile，
# 見稽核 W-7）；舊名保留 alias，跨檔呼叫端不必立即改。
_vol_pctile = vol_pctile


def _score_technical_high(row, df) -> dict:
    """技術衰竭（max 30）= 頂背離(20) + RSI_14 超買(10)。複用既有 divergence（通用）。"""
    div = detect_top_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    if n >= 2: d_s, d_l = 20, "🔴 RSI+MACD 雙頂"
    elif n == 1: d_s, d_l = round(9 + 5 * div.get("strength", 0.0)), "🟠 單指標頂"
    else: d_s, d_l = 0, "⚪ 無"
    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    if _nan(rsi): r_s, r_l = 0, "⚪ 無資料"
    elif rsi >= 80: r_s, r_l = 10, f"🔴 極度超買({rsi:.0f})"
    elif rsi >= 70: r_s, r_l = 6, f"🟠 超買({rsi:.0f})"
    else: r_s, r_l = 0, f"⚪ 中性({rsi:.0f})"
    return {"score": d_s + r_s, "max": 30, "label": f"背離 {d_l}；RSI {r_l}",
            "note": "頂背離（價HH/指標LH）+ RSI 超買", "sub": {"div_n": n, "rsi": (None if _nan(rsi) else float(rsi))}}


def _score_valuation_high(valuation) -> dict:
    """估值過高（max 30，校準最強維）= PE 高(16) + PB 高(14)。絕對值分級。"""
    if not valuation:
        return {"score": 0, "max": 30, "label": "估值 ⚪ 無資料（上櫃/缺）",
                "note": "PE/PB 絕對值（逃頂最強維 AUC~0.63）", "sub": {}}
    pe, pb = valuation.get("pe"), valuation.get("pb")
    if _nan(pe) or pe <= 0: pe_s, pe_l = 0, "⚪ 無/負"
    elif pe >= 40: pe_s, pe_l = 16, f"🔴 {pe:.0f} 偏貴(≥40)"
    elif pe >= 25: pe_s, pe_l = 9, f"🟠 {pe:.0f} 偏高(≥25)"
    elif pe >= 18: pe_s, pe_l = 4, f"🟡 {pe:.0f} 略高(≥18)"
    else: pe_s, pe_l = 0, f"⚪ {pe:.0f}"
    if _nan(pb) or pb <= 0: pb_s, pb_l = 0, "⚪ 無"
    elif pb >= 5: pb_s, pb_l = 14, f"🔴 {pb:.1f} 偏貴(≥5)"
    elif pb >= 3: pb_s, pb_l = 8, f"🟠 {pb:.1f} 偏高(≥3)"
    elif pb >= 2: pb_s, pb_l = 3, f"🟡 {pb:.1f} 略高(≥2)"
    else: pb_s, pb_l = 0, f"⚪ {pb:.1f}"
    return {"score": pe_s + pb_s, "max": 30, "label": f"PE {pe_l}；PB {pb_l}",
            "note": "PE/PB 絕對值過高（swing 逃頂 AUC PE 0.627/PB 0.640，絕對勝分位）",
            "sub": {"pe": pe, "pb": pb}}


def _score_volume_high(df, *, forming_last: bool = False) -> dict:
    """量能見頂（max 18，v0.3 新增最強新維）= 近 `VOL_WINDOW` 日均量處個股自身高分位（爆量見頂）。
    swing 逃頂 AUC 0.648、跨 labeling 0.61–0.67、抄底側 0.49–0.51（不對稱，非移動幅度混淆）；
    均量視窗 5 日與單日判別力等價、判讀更穩（2026-08-10 校準，見 `vol_pctile`）。
    forming_last=True（live 盤中）→ 排除未結算的今日棒，均量只用已結算日。"""
    pct = vol_pctile(df, drop_last=forming_last)
    if pct is None:
        return {"score": 0, "max": 18, "label": "量能 ⚪ 資料不足", "note": _VOL_NOTE, "sub": {}}
    if pct >= 0.95: s, l = 18, f"量能 🔴 爆量 {pct*100:.0f}分位（見頂）"
    elif pct >= 0.85: s, l = 12, f"量能 🟠 量增 {pct*100:.0f}分位"
    elif pct >= 0.70: s, l = 6, f"量能 🟡 偏高 {pct*100:.0f}分位"
    else: s, l = 0, f"量能 ⚪ {pct*100:.0f}分位（正常）"
    return {"score": s, "max": 18, "label": l, "note": _VOL_NOTE,
            "sub": {"vol_pctile": pct, "vol_window": VOL_WINDOW, "forming_excluded": forming_last}}


def _score_leverage_high(margin) -> dict:
    """槓桿過熱（max 10，〔弱〕）= 融資餘額增速高（散戶追高加槓桿）。"""
    if not margin or margin.get("fin_chg_pct") is None:
        return {"score": 0, "max": 10, "label": "融資 ⚪ 無資料",
                "note": "融資增速高＝散戶追高〔弱 AUC 0.538〕", "sub": {}}
    chg = margin["fin_chg_pct"]
    if chg >= 5: s, l = 10, f"融資 🔴 暴增 {chg:+.1f}%（散戶追高）"
    elif chg >= 3: s, l = 7, f"融資 🟠 大增 {chg:+.1f}%"
    elif chg >= 1: s, l = 3, f"融資 🟡 增 {chg:+.1f}%"
    else: s, l = 0, f"融資 ⚪ {chg:+.1f}%（未過熱）"
    return {"score": s, "max": 10, "label": l, "note": "融資餘額日變化〔弱 AUC 0.538〕",
            "sub": {"fin_chg_pct": chg}}


def _score_institution_high(institutional, df) -> dict:
    """法人派發（max 4，〔弱/雜訊〕）= 三大法人賣超（近20日均量正規化）。"""
    if not institutional or institutional.get("total_net") is None:
        return {"score": 0, "max": 4, "label": "法人 ⚪ 無資料",
                "note": "三大法人賣超〔弱 AUC 0.519，降權〕", "sub": {}}
    net = institutional["total_net"]
    av = _avg_vol(df)
    ratio = (net / av * 100) if av else None
    if ratio is None:
        s, l = (2, "法人 🟠 賣超（無量基準）") if net < 0 else (0, "法人 ⚪ 買超/平")
    elif ratio <= -20: s, l = 4, f"法人 🟠 大賣 {ratio:+.0f}%均量"
    elif ratio <= -8: s, l = 2, f"法人 🟡 賣超 {ratio:+.0f}%均量"
    elif ratio <= -3: s, l = 1, f"法人 🟡 小賣 {ratio:+.0f}%均量"
    else: s, l = 0, f"法人 ⚪ {ratio:+.0f}%均量（買/平）"
    return {"score": s, "max": 4, "label": l, "note": "三大法人買賣超/均量〔弱 AUC 0.519〕",
            "sub": {"total_net": net, "ratio_pct": ratio}}


def _score_tdcc_high(tdcc) -> dict:
    """籌碼鬆動（max 8，〔弱・樣本薄〕）= TDCC 散戶持股比高（籌碼分散＝派發末端）。"""
    if not tdcc or tdcc.get("retail_pct") is None:
        return {"score": 0, "max": 8, "label": "散戶 ⚪ 無集保資料",
                "note": "散戶持股比高〔弱 AUC 0.538・樣本薄〕", "sub": {}}
    rp = tdcc["retail_pct"]
    if rp >= 40: s, l = 8, f"散戶 🔴 {rp:.0f}%（籌碼鬆散）"
    elif rp >= 30: s, l = 5, f"散戶 🟠 {rp:.0f}%"
    elif rp >= 25: s, l = 2, f"散戶 🟡 {rp:.0f}%"
    else: s, l = 0, f"散戶 ⚪ {rp:.0f}%（集中）"
    return {"score": s, "max": 8, "label": l, "note": "TDCC 散戶（≤50張）持股比〔弱・樣本薄〕",
            "sub": {"retail_pct": rp, "major_pct": tdcc.get("major_pct")}}


def compute_relative_high_tw(row, df=None, *, chip=None,
                             forming_last: bool = False) -> Tuple[int, Dict[str, dict]]:
    """台股相對高點七維評分（0–100，clamp）。chip = service.tw_chip.get_chip_bundle 結果（可缺）。

    forming_last=True：df 最後一根是「今日進行式」日棒（live 盤中，見
    `service.ohlc_universal.is_daily_bar_forming`）→ **三個吃成交量的維度**（量能見頂、量價背離、
    法人派發的均量分母）改以最後一根已結算日棒為準，不用只累積到當下的當日量去比歷史整日量
    （理由見 `vol_pctile`）。改動時三個都要顧：漏掉 institution 會讓法人賣超/均量的分母偏小。
    價格類維度（技術衰竭）仍用進行式那根＝當下價，語意本就該即時。
    回測/校準腳本餵的是 EOD 日線，維持預設 False（PiT 口徑不變）。"""
    chip = chip or {}
    df_settled = df.iloc[:-1] if (forming_last and df is not None and len(df) >= 2) else df
    signals = {
        "technical": _score_technical_high(row, df),
        "valuation": _score_valuation_high(chip.get("valuation")),
        "volume": _score_volume_high(df, forming_last=forming_last),
        "leverage": _score_leverage_high(chip.get("margin")),
        "institution": _score_institution_high(chip.get("institutional"), df_settled),
        "tdcc": _score_tdcc_high(chip.get("tdcc")),
        "vol_price": rescale_dim(score_volume_price_top(df_settled), WEIGHTS_HIGH_TW["vol_price"]),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_high_tw_meta(score: int) -> Tuple[str, str, str]:
    return high_meta_ladder(score, "技術＋估值俱過熱，分批止盈/減碼")
