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
    dovish flags（通膨/就業）2026-07 已用 FRED point-in-time 回測（tests/relative_low_macro_backtest.py）：
    對相對底部 全期 AUC 0.448（方向反）/ 費率era 0.562（弱）→ 落後確認非領先訊號 → WEAK_SUBDIMS_LOW，
    維持低權規則式（PENDING 清空）。UNFITTED_DIMS_LOW 仍為空。
  - derivatives 負費率子項 2026-06 已回歸重校門檻（AUC 0.626，判別帶在淺負；見 funding_threshold_calib）。
"""
import math
from typing import Optional, Dict, Any, Tuple

import numpy as np
import pandas as pd

from core.divergence import detect_bottom_divergence_combo
from core.relative_high import annualize_funding
from core.pit_ladder import pit_percentile, percentile_score, percentile_label


# ══════════════════════════════════════════════════════════════════════════════
# 常數（單一來源；BTC_WATCH.py path import 直接取用，杜絕兩邊閾值漂移）
# ══════════════════════════════════════════════════════════════════════════════

# 負資金費率年化門檻（2026-06 回歸重校，見 tests/funding_threshold_calib.py）：
# 負費率→底 AUC 0.626，但判別力在「淺負」(Youden 最佳 ≤-3%)；≤-15/-20/-30% 歷史僅 8/4/3 日(危機)，
# 召回崩到 3% → 大分不該鎖在深負。主判別帶下移至 -2~-5%、滿分線上修至 -20%（仍給危機級洗盤最高分）。
FUNDING_ANN_LOW_YELLOW = -5.0    # 年化 % — 明顯空方付費（主判別帶；黃）
FUNDING_ANN_LOW_RED    = -20.0   # 年化 % — 極端空方付費（危機級洗盤；紅）

# ── PiT 滾動分位（2026-08-25 提出 → 同日**撤回計分**，僅留觀測值）────────────
# 動機：2024-12-10 之後幣安 BTCUSDT 資費再未越過 0.01%/8h 基準（=年化 10.95%），
# 全分布壓在 [-11%, +11%] 年化 → 下方絕對門檻在此環境命中率崩壞（≤-20% 命中 0 日、
# ≤-10% 僅 3 日），負費率子項 623 日只有 15.4% 的日子非零。
# 曾改為「絕對階梯 ∪ PiT 分位」取大值，但**獨立檢核後撤回**，經過如下：
#   初版宣稱 holdout 混合 0.546 vs 絕對 0.487 → 修掉兩個缺陷後翻盤：
#     (a) 未來窗不足 60 日的樣本未剔除（holdout 16 個正樣本有 3 個只有 11~55 日窗）
#     (b) 第五檔 cutoff 寫 25.0，**不是**稀有度預算換算值（ann<0 的 train 稀有度是 12.96%）
#   歸因（只改其一）：(a) 單獨修 → 混合仍贏（holdout 0.610 vs 0.499）；
#                    (b) 單獨修 → 混合就輸（0.509 vs 0.521）→ **優勢全來自那個自選參數**。
#   兩項都修後：train 混合 0.640 vs 絕對 0.601（train 是選參數的那份，不算證據），
#              holdout 混合 0.486 vs 絕對 0.499，且**換 8 個對照抽樣種子 0/8 混合勝**。
# → 依 CONSTITUTION 第 11 條（樣本少不 grid search、0.55 收案門檻）**不予採用**：
#   負費率子項維持純絕對階梯。分位僅作為 sub["funding_pct"] 的觀測值輸出，
#   **不計分、不進面板 label**（避免變成第 11 條所禁的誤導性「參考顯示」）。
#   ⚠️ 這不代表分位法錯，只代表「用現有樣本證明不了」。資料長大後重跑
#   tests/funding_percentile_calib.py 再議；**重議前不要只看 train 就改回去**。
FUNDING_PCT_WINDOW  = 180     # 觀測用滾動視窗（日）
FUNDING_PCT_MIN_OBS = 90      # 視窗內最少樣本，不足則不輸出分位
# 五檔全部由 train 稀有度換算（僅供未來重議時參考，目前不計分）：
FUNDING_PCT_LOW_CUTS = (1.0, 3.3, 5.5, 9.3, 13.0)   # → 10 / 8 / 6 / 3 / 1 分

# ── 抄底分級門檻（具名常數；2026-08-25 重校）──────────────────────────────────
# 抽成常數的理由同 relative_high 的 TOP_LEVEL_*：杜絕別處再抄一份數字然後靜默漂移。
LOW_LEVEL_STRONG  = 56   # 🟢 強力抄底訊號（實際分布 P99.5）
LOW_LEVEL_VALUE   = 54   # 🟢 明確低估（P99）
LOW_LEVEL_COOL    = 45   # 🟡 偏冷觀察
LOW_LEVEL_NEUTRAL = 26   # ⚪ 中性
# ⛔ 實證否決區（2026-08-26 新增，**唯一通過獨立驗收的雷達用法**）
# 設計：BTCUSDT 全樣本（`tests/radar_veto_filter.py`）——分數 <=5 的日子否決 31.3%，
#       其後 180 日報酬中位 +1.3% vs 未否決 +27.5%，Mann-Whitney p=2.0e-07。
# 獨立驗收：改測**非 BTC 幣對**（`tests/veto_rules_acceptance.py`，標的與分數組成都不同
#       ——MVRV/SOPR/ETF 為 BTC 專屬故為 None，正是生產上 cap 72 的實際輸入）：
#       ETH −15.6%／SOL −16.3%／BNB −11.8%／XRP −32.5%，**4/4 方向一致**，
#       跨檔中位 −16.0%、否決比例中位 34.6%。三條事前判準全過。
# ⚠️ 這是**否決條件不是進場條件**：它說的是「這種日子別進場」，
#    不是「分數高就該進場」——後者在 2026-08-26 的 holdout 已被否決（見 relative_high.py 檔頭）。
# ⚠️ 6~25 分沿用舊的「無底部訊號」文案，那一段**沒有通過驗證**
#    （<=10/15/20/25 的否決比例 44~71%，事前判準 V3 全未過）→ 兩段刻意分開顯示，別再合併。
LOW_VETO_VALIDATED = 5

# 六維權重（各維最高分；理論總和 106，compute_relative_low_score clamp 到 100）
# — 經 relative_low_backtest 拍板（實證導向）
WEIGHTS_LOW = {
    "cycle":       25,   # 一、長週期深跌（Mayer 13 + 200週 12）← 冪律 2026-08-25 移除，6 分按比例併回
    "derivatives": 20,   # 二、合約超冷（負費率 10 + OI 滾動清洗 10）
    "technical":   20,   # 三、技術回穩（底背離 14 + RSI 超賣 6，RSI 2026-08-25 改絕對∪分位）
    "sentiment":   15,   # 四、情緒恐慌（F&G 10 + BTC.D 上升 5，F&G 2026-08-25 改絕對∪分位）
    "onchain":     16,   # 五、鏈上吸籌（ETF 連續流入 6 + SOPR 割肉 4 + MVRV-Z 深度低估 6，見下方 2026-07 驗證）
    "macro":       10,   # 六、總經順風（降息/鴿派 7 + 事件臨近 3）灰燈
}

# 2026-07 MVRV-Z 抄底側驗證（tests/relative_ref_signals_backtest.py，本地已快取
# db/bottom_metrics_cache.json 的 mvrv_zscore，2022-07+，零新網路請求）：swing 低點 order=10、
# 60日內反彈≥18% 為正樣本，n_pos=23/n_neg=104，用 -z 當單調子分數（值越低越像底）→
# AUC=0.732，**比現役 SOPR(0.585) 還強**，過 0.55 門檻 → 從「參考顯示」（原
# reference_low_signals 的 mvrv_z 分支）轉正式計分子項，配重給到跟 ETF 同級(6)。
#
# 同批驗證的 Hash Ribbons（礦工投降強度 (SMA60-SMA30)/SMA60）— n=191，AUC=0.359，方向反/無效。
# 原本以「參考顯示、不計分」保留在 watcher 面板，但顯示的「礦工投降中→打底醞釀」本身就是那個
# 被證明方向相反的訊號（無中性事實價值、易誤讀成偏多），2026-07 已整段移除（_hash_ribbon_read /
# reference_low_signals 一併刪除）。不代表 Hash Ribbons 理論錯誤，可能是「黃金交叉事件」比本次測的
# 「持續投降深度」更有預測力，但那是另一個假設，需另設計「事件式」樣本，不在本次範圍。**勿再加回。**

# 維度狀態標示（兩種不同性質，介面以不同 tag 呈現）：
#   UNFITTED_DIMS_LOW   ＝權重採專家設定、歷史樣本不足「待累積後回測」即可擬合（如 OI 自建快照）。
#   RULE_BASED_DIMS_LOW ＝子項本質為規則式、不可統計擬合（macro 的 event-window 事件臨近）。
# onchain：2026-06 敏感度驗證通過，已不在任一清單（見 backtest validate_unfitted_dims）。
# macro：拆兩子維 — event-window(事件臨近)＝規則式(永久不可擬合)；dovish flags(通膨/就業)＝
#        2026-07 已在家用網路（FRED 可達）回測（tests/relative_low_macro_backtest.py）：
#        point-in-time（含發布延遲，無前視）dovish_score 對相對底部 全期 AUC=0.448（方向反，
#        底部觸發率反低於非底）、資金費率時代 AUC=0.562（弱）；增量在實際 macro 權重(λ≈0.07)
#        下 Δ≈+0.02 可忽略。結論：dovish 為「落後確認」非「領先底部訊號」（底部領先 macro 改善）
#        → 不給實證權重，維持低權/規則式灰燈。已測畢，PENDING 清空。
#        （逃頂側 hawkish flags 對相對頂部 AUC=0.607/0.660 明確有效 — 頂部與升息環境同步。）
UNFITTED_DIMS_LOW = ()
RULE_BASED_DIMS_LOW = ("macro",)
# dovish flags 回測已完成（2026-07，弱維/落後確認）→ 不再列「待回補」；保留鍵供文件追溯。
PENDING_FIT_SUBDIMS_LOW = {}
WEAK_SUBDIMS_LOW = {
    "macro": "dovish flags（通膨/就業）FRED 回測=弱/落後確認（全期 AUC 0.45、費率era 0.56），維持低權規則式",
    "derivatives": ("負費率子項＝純絕對階梯；holdout AUC 0.499（n_pos=13）＝無訊號。"
                    "PiT 分位混合版已於 2026-08-25 撤回（優勢全來自未揭露的自選 cutoff，"
                    "修正後 8 個種子 0/8 勝）→ 待樣本長大重測"),
}


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


# ══════════════════════════════════════════════════════════════════════════════
# 六維子評分
# ══════════════════════════════════════════════════════════════════════════════

def _score_cycle(row) -> dict:
    """
    長週期深跌（max 25）= Mayer(13) + 200週均比(12)。

    2026-08-25 **移除冪律子項**（原 6 分），理由三條，任一條單獨都夠：
      1. 觸發率 100%、滿分率 92.8%、AUC 0.500 → 是常數不是訊號
      2. 「0 分」需 PowerLaw_Ratio >= 5.0，而全期最大僅 3.12 → **該檔結構上不可觸及**
      3. 標籤誤導：`PowerLaw_Ratio = close / PowerLaw_Support`，2026-08-25 實值 0.47
         （support 約 $169,824、價格 $79,744 ＝價格只有那條線的 47%），面板卻寫「🟢 貼近冪律支撐」
    重訂階梯也救不了：混合版 0.516/0.500、純分位取代版 0.499/0.414（都不優於現行）
    → 情境同 CONSTITUTION 第 11 條處理 Hash Ribbons 的前例（顯示本身就在誤導）→ 整段移除。
    原 6 分**按比例併回同維度**（10:9 → 13:12），維持 cycle=25 不動總分刻度。
    `PowerLaw_Ratio` 仍在 sub 保留原始值供機器讀取，但不計分、不進面板文字。
    """
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
        if   mayer < 0.8: m_s, m_lbl = 13, "🟢 低於2年均線×0.8 (極度低估)"
        elif mayer < 1.0: m_s, m_lbl = 8,  "🟡 低於2年均線"
        elif mayer < 1.2: m_s, m_lbl = 4,  "⚪ 略低於均線"
        else:             m_s, m_lbl = 0,  "⚪ 高於均線"

    if sma200w is None:
        s_s, s_lbl, s_val = 0, "⚪ 累積中(需200週)", "—"
    else:
        s_val = f"{sma200w:.2f}x"
        if   sma200w < 1.0: s_s, s_lbl = 12, "🟢 跌破200週均 (歷史絕對底)"
        elif sma200w < 1.3: s_s, s_lbl = 8,  "🟡 接近200週均"
        elif sma200w < 2.0: s_s, s_lbl = 4,  "⚪ 正常範圍"
        else:               s_s, s_lbl = 0,  "⚪ 偏高"

    return {
        "value": f"Mayer {m_val}｜200週 {s_val}",
        "score": m_s + s_s, "max": WEIGHTS_LOW["cycle"],
        "label": f"Mayer {m_lbl}；200週 {s_lbl}",
        "note": "長週期估值深跌（Mayer/200週）— 冪律子項 2026-08-25 因常亮無鑑別力移除",
        "sub": {"mayer": mayer, "mayer_score": m_s, "sma200w": sma200w,
                "sma200w_score": s_s, "powerlaw": pl, "powerlaw_score": 0},
    }


def funding_pit_percentile(hist, current=None, min_obs: int = FUNDING_PCT_MIN_OBS,
                           window: int = FUNDING_PCT_WINDOW):
    """
    資費分位（目前只作為 sub["funding_pct"] 觀測值，**不計分**）。

    2026-08-25 獨立檢核 🟠 No.5：本函式原本是 core/pit_ladder.pit_percentile 的
    逐行複製，與「單一真實來源」的宣稱相左 → 改為委派，只保留資費專屬的預設參數。
    """
    return pit_percentile(hist, current, min_obs=min_obs, window=window)


def _score_derivatives_low(funding_8h, oi_stats, funding_ann_hist=None) -> dict:
    """
    合約超冷（max 20）= 負資金費率(10) + OI 滾動清洗(10)。

    負費率子項 2026-08-25 起為「絕對階梯 ∪ PiT 滾動分位」**取大值**：
      絕對階梯抓「跨環境的絕對極端」（2020/2022 危機級深負），分位抓「當前環境內的相對極端」
      （後 2024-12 低費率環境，絕對階梯已失效 → 見 FUNDING_PCT_* 常數上方校準紀錄）。
    funding_ann_hist=None（呼叫端沒餵歷史）→ 自動退回純絕對階梯，行為同 2026-08 之前。
    """
    ann = annualize_funding(funding_8h)
    # 資費抓不到就不該回報分位：否則 pct 會退化成「歷史最後一筆的分位」，
    # 面板顯示一個與今日無關的數字（2026-08-25 獨立檢核 🟡 No.9）。
    pct = None
    if ann is None:
        f_s, f_lbl, f_val = 0, "⚪ 無資料", "—"
    else:
        pct = funding_pit_percentile(funding_ann_hist, ann) if funding_ann_hist else None
        f_val = f"{ann:.0f}% (年化)"
        # 階梯由 funding_threshold_calib.py 回歸：判別帶在淺負(≤-2~-5%)，深負(≤-20%)為危機級洗盤滿分
        if   ann <= FUNDING_ANN_LOW_RED:    f_a, f_lbl = 10, "🟢 極端空方付費 (≤-20% 年化, 危機洗盤)"
        elif ann <= -10:                    f_a, f_lbl = 8,  "🟢 嚴重空方付費 (≤-10%)"
        elif ann <= FUNDING_ANN_LOW_YELLOW: f_a, f_lbl = 6,  "🟡 空方付費 (≤-5%)"
        elif ann <= -2:                     f_a, f_lbl = 3,  "🟡 偏空 (≤-2%)"
        elif ann < 0:                       f_a, f_lbl = 1,  "⚪ 微負費率"
        else:                               f_a, f_lbl = 0,  "⚪ 多方付費/中性"

        # 分位側 2026-08-25 已撤回計分（見上方常數區的歸因紀錄）：只算不給分、不進 label。
        # 保留計算是為了讓 sub 有觀測值可累積，將來樣本夠了能直接重跑校準。
        f_s = f_a

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
        "note": ("OI 1h 滾動清洗未擬合(歷史不足)；負費率＝純絕對階梯"
                 "(holdout AUC 0.499＝無訊號；分位混合版已撤回，見 FUNDING_PCT_* 紀錄)"),
        "sub": {"funding_ann": ann, "funding_score": f_s,
                "funding_pct": pct, "funding_pct_window": FUNDING_PCT_WINDOW,
                "oi_change_1h_pct": chg, "oi_score": o_s},
    }


def _score_technical_low(row, df, rsi_pct_enabled: bool = True) -> dict:
    """
    技術回穩（max 20）= 底背離 RSI+MACD(14) + RSI_14 超賣(6)。

    RSI 子項 2026-08-25 改「絕對階梯 ∪ PiT 滾動分位」取大值（見 core/pit_ladder）：
      絕對門檻 <=20/25/30 在測期間只有 4.2% 的日子觸發，把原始 RSI 的 AUC 0.782 壓到 0.559。
      改混合後 train 0.538→0.693、holdout 0.547→0.837（**雙邊都贏才收案**）。
    歷史直接取自已傳入的 df，**不需要新資料管線**。
    """
    div = detect_bottom_divergence_combo(df) if df is not None else {"n_confirm": 0, "strength": 0.0}
    n = div.get("n_confirm", 0)
    strength = div.get("strength", 0.0)
    if   n >= 2: d_s, d_lbl = 14, "🟢 RSI+MACD 雙底"
    elif n == 1: d_s, d_lbl = round(6 + 4 * strength), "🟡 單指標底"
    else:        d_s, d_lbl = 0, "⚪ 無"

    rsi = row.get("RSI_14") if hasattr(row, "get") else None
    r_pct = None
    if _nan(rsi):
        r_s, r_lbl, r_val = 0, "⚪ 無資料", "—"
    else:
        r_val = f"{rsi:.0f}"
        if   rsi <= 20: r_a, r_lbl = 6, f"🟢 極度超賣({r_val})"
        elif rsi <= 25: r_a, r_lbl = 4, f"🟡 超賣({r_val})"
        elif rsi <= 30: r_a, r_lbl = 2, f"⚪ 偏超賣({r_val})"
        else:           r_a, r_lbl = 0, f"⚪ 中性({r_val})"
        # rsi_pct_enabled=False（非 BTC 幣對）→ 不套分位：級距在 BTCUSDT 上校準，
        # 其他幣對的 RSI 分布不同，套上去等於用別人的刻度量自己（獨立檢核 🟠 No.7）。
        if rsi_pct_enabled and df is not None and "RSI_14" in getattr(df, "columns", []):
            r_pct = pit_percentile(list(df["RSI_14"].values), rsi)
        r_p = percentile_score(r_pct, 6, high_is_extreme=False)
        r_s = max(r_a, r_p)
        if r_p > r_a and r_pct is not None:
            r_lbl = f"🟡 本環境偏低({r_val}，{percentile_label(r_pct)})"
        elif r_pct is not None:
            r_lbl = f"{r_lbl} {percentile_label(r_pct)}"

    return {
        "value": f"背離×{n}｜RSI {r_val}",
        "score": d_s + r_s, "max": WEIGHTS_LOW["technical"],
        "label": f"背離 {d_lbl}；RSI {r_lbl}",
        "note": "日線底背離（價LL/指標HL）+ RSI_14（絕對∪PiT分位，2026-08-25 重訂）",
        "sub": {"divergence_n": n, "divergence_strength": strength,
                "divergence_score": d_s, "rsi": (None if _nan(rsi) else float(rsi)),
                "rsi_score": r_s, "rsi_pct": r_pct},
    }


def _score_sentiment_low(fng, btc_d_trend, fng_hist=None) -> dict:
    """
    情緒恐慌（max 15）= F&G 極度恐懼(10) + BTC.D 上升避險(5)。

    F&G 子項 2026-08-25 改「絕對階梯 ∪ PiT 滾動分位」取大值：
      train 0.525→0.584、holdout 0.568→0.718（雙邊都贏才收案）。
    fng_hist=None 時自動退回純絕對階梯（向後相容）。
    """
    g_pct = None
    if _nan(fng):
        g_s, g_lbl, g_val = 0, "⚪ 無資料", "—"
    else:
        g_val = f"{fng:.0f}"
        if   fng <= 10: g_a, g_lbl = 10, f"🟢 極度恐懼({g_val})"
        elif fng <= 20: g_a, g_lbl = 8,  f"🟢 恐懼({g_val})"
        elif fng <= 25: g_a, g_lbl = 5,  f"🟡 偏恐懼({g_val})"
        elif fng <= 30: g_a, g_lbl = 3,  f"⚪ 偏空({g_val})"
        else:           g_a, g_lbl = 0,  f"⚪ 中性/貪婪({g_val})"
        g_pct = pit_percentile(fng_hist, fng) if fng_hist else None
        g_p = percentile_score(g_pct, 10, high_is_extreme=False)
        g_s = max(g_a, g_p)
        if g_p > g_a and g_pct is not None:
            g_lbl = f"🟡 本環境偏恐懼({g_val}，{percentile_label(g_pct)})"
        elif g_pct is not None:
            g_lbl = f"{g_lbl} {percentile_label(g_pct)}"

    if not btc_d_trend or btc_d_trend.get("change_pp") is None:
        b_s, b_lbl, b_val = 0, "⚪ 累積中", "—"
    else:
        chg = btc_d_trend["change_pp"]
        b_val = f"{chg:+.1f}pp"
        if   btc_d_trend.get("is_rising") or chg >= 1.0: b_s, b_lbl = 5, f"🟢 上升({b_val}) 避險回流"
        elif chg >= 0.5:                                 b_s, b_lbl = 3, f"🟡 偏強({b_val})"
        else:                                            b_s, b_lbl = 0, f"⚪ 穩定/下降({b_val})"

    return {
        "value": f"F&G {g_val}｜BTC.D {b_val}",
        "score": g_s + b_s, "max": WEIGHTS_LOW["sentiment"],
        "label": f"F&G {g_lbl}；BTC.D {b_lbl}",
        "note": "恐懼貪婪極度恐懼 + BTC.D 上升（資金避險回流主鏈）",
        "sub": {"fng": (None if _nan(fng) else float(fng)), "fng_score": g_s,
                "fng_pct": g_pct,
                "btcd_change_pp": (btc_d_trend or {}).get("change_pp"), "btcd_score": b_s},
    }


def _score_onchain_low(etf_summary, sopr, mvrv_z=None, sopr_hist=None) -> dict:
    """
    鏈上吸籌（max 16）= ETF 連續淨流入(6) + SOPR 割肉投降(4) + MVRV-Z 深度低估(6)。
    ETF 灰燈/未擬合；SOPR/MVRV-Z 皆已驗證（AUC 0.585/0.732，見 WEIGHTS_LOW 上方註解）。

    SOPR 子項 2026-08-25 改「絕對階梯 ∪ PiT 滾動分位」取大值：
      絕對門檻 <=0.92/0.95/0.98 只有 2.8% 的日子觸發，把原始 SOPR 的 AUC 0.707 壓到 0.568。
      改混合後 train 0.600→0.735、holdout 0.538→0.597（雙邊都贏才收案）。
    MVRV-Z **未改**：混合與取代版都沒有雙邊贏（見 tests/ladder_redesign_calib.py）。
    sopr_hist=None 時自動退回純絕對階梯（向後相容）。
    """
    if not etf_summary or etf_summary.get("n", 0) == 0:
        e_s, e_lbl, e_val = 0, "⚪ 無資料源", "—"
    else:
        days = etf_summary.get("consecutive_inflow_days", 0)
        e_val = f"連{days}日流入"
        if   days >= 7: e_s, e_lbl = 6, "🟢 連續流入≥7日 (機構吸籌)"
        elif days >= 5: e_s, e_lbl = 4, "🟢 連續流入≥5日"
        elif days >= 3: e_s, e_lbl = 2, "🟡 連續流入≥3日"
        else:           e_s, e_lbl = 0, "⚪ 無顯著流入"

    s_pct = None
    if _nan(sopr):
        s_s, s_lbl, s_val = 0, "⚪ 無資料源", "—"
    else:
        s_val = f"{sopr:.3f}"
        if   sopr <= 0.92: s_a, s_lbl = 4, f"🟢 深度割肉({sopr:.2f}) 投降"
        elif sopr <= 0.95: s_a, s_lbl = 3, f"🟢 割肉({sopr:.2f})"
        elif sopr <= 0.98: s_a, s_lbl = 2, f"🟡 微虧賣出({sopr:.2f})"
        else:              s_a, s_lbl = 0, f"⚪ 中性/獲利({sopr:.2f})"
        s_pct = pit_percentile(sopr_hist, sopr) if sopr_hist else None
        s_p = percentile_score(s_pct, 4, high_is_extreme=False)
        s_s = max(s_a, s_p)
        if s_p > s_a and s_pct is not None:
            s_lbl = f"🟡 本環境偏低({sopr:.2f}，{percentile_label(s_pct)})"
        elif s_pct is not None:
            s_lbl = f"{s_lbl} {percentile_label(s_pct)}"

    if _nan(mvrv_z):
        m_s, m_lbl, m_val = 0, "⚪ 無資料源", "—"
    else:
        m_val = f"{mvrv_z:.2f}"
        if   mvrv_z <= 0: m_s, m_lbl = 6, f"🟢 歷史底部區({mvrv_z:.1f})"
        elif mvrv_z <= 1: m_s, m_lbl = 4, f"🟢 深度低估({mvrv_z:.1f})"
        elif mvrv_z <= 2: m_s, m_lbl = 2, f"🟡 偏低({mvrv_z:.1f})"
        else:             m_s, m_lbl = 0, f"⚪ 中性/偏高({mvrv_z:.1f})"

    return {
        "value": f"ETF {e_val}｜SOPR {s_val}｜MVRV-Z {m_val}",
        "score": e_s + s_s + m_s, "max": WEIGHTS_LOW["onchain"],
        "label": f"ETF {e_lbl}；SOPR {s_lbl}；MVRV-Z {m_lbl}",
        "note": "SOPR(AUC 0.585)/MVRV-Z(AUC 0.732)已驗證；ETF 連續淨流入 2024+ 資料薄沿用專家權重",
        "sub": {"etf_consecutive_inflow": (etf_summary or {}).get("consecutive_inflow_days"),
                "sopr_pct": s_pct,
                "etf_score": e_s, "sopr": (None if _nan(sopr) else float(sopr)),
                "sopr_score": s_s, "mvrv_z": (None if _nan(mvrv_z) else float(mvrv_z)),
                "mvrv_z_score": m_s},
    }


def _score_macro_low(macro) -> dict:
    """
    總經順風（max 10）= 降息/鴿派(7) + 事件臨近(3)。灰燈/未擬合。
    macro dict（皆選填）：cpi_cool / pce_cool / jobs_weak : bool；event_within_days : int。
    降息/流動性寬鬆 → 高風險資產回流 → BTC 順風（逃頂 macro 的反向）。
    """
    if not macro:
        return {"value": "—", "score": 0, "max": WEIGHTS_LOW["macro"],
                "label": "總經 ⚪ 無資料源",
                "note": "事件臨近=規則式(不可擬合)；通膨/就業 dovish=FRED 回測弱/落後確認(低權)",
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
        "label": ("總經 🟢 " + "、".join(bits)) if bits else "總經 ⚪ 中性",
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
    mvrv_z: Optional[float] = None,
    funding_ann_hist=None,
    fng_hist=None,
    sopr_hist=None,
    rsi_pct_enabled: bool = True,
) -> Tuple[int, Dict[str, dict]]:
    """
    相對底部六維綜合評分（0–100）。鏡像 relative_high.compute_escape_top_score。
    回傳 (score:int, signals:dict[dim] = {value,score,max,label,note,sub})。

    row：最新日線（含 RSI_14 / Mayer_Multiple / SMA200W_Ratio / PowerLaw_Ratio 等，
         需先過 indicators + bear_bottom）。df：完整日線（底背離用）。
    其餘為呼叫端算好的純量/dict（本層零網路請求 → 易測、可被 BTC_WATCH 自抓資料餵入）。
    mvrv_z：2026-07 已驗證計入 onchain 子分（AUC 0.732，見 WEIGHTS_LOW 上方註解），不再是純參考。
    """
    signals = {
        "cycle":       _score_cycle(row),
        "derivatives": _score_derivatives_low(funding_8h, oi_stats, funding_ann_hist),
        "technical":   _score_technical_low(row, df, rsi_pct_enabled),
        "sentiment":   _score_sentiment_low(fng, btc_d_trend, fng_hist),
        "onchain":     _score_onchain_low(etf_summary, sopr, mvrv_z, sopr_hist),
        "macro":       _score_macro_low(macro),
    }
    score = int(sum(s["score"] for s in signals.values()))
    score = max(0, min(100, score))
    return score, signals


def relative_low_meta(score: int) -> Tuple[str, str, str]:
    """
    (等級, 顏色, 操作建議) — 鏡像 escape_top_meta 的反向。

    2026-08-25 重校最高兩級（原 75／60）：實測 2542 日，抄底總分最高只到 65，
    `>=75` 稀有度 **0.00%**、`>=60` 僅 0.20%；而**真實底部當天的中位分只有 26、最高 53**
    ——最高兩級在真底也從沒到過，是死檔位。
    新門檻錨在實際分布的 P99.5／P99（56／54 → 稀有度 0.67%／1.53%）；
    下兩級（45／26）稀有度正常（10.15%／35.76%），僅 25→26 隨階梯重訂微調。
    ⚠️ in-sample 校準，**只影響分級文案、不進任何交易條件**。
    ⚠️ 校準口徑同 escape_top_meta：重放少餵 btc_d_trend/macro，生產分數會系統性偏高。

    2026-08-26 於最低端新增第六級 `⛔ 實證否決區`（<=LOW_VETO_VALIDATED=5）。
    **這是本雷達唯一通過獨立驗收的用法**，證據與界線見 LOW_VETO_VALIDATED 上方註解。
    刻意與 6~25 的「🔴 無底部訊號」分開：後者是既有文案、**未經驗證**，
    合併顯示會讓使用者以為兩段有同等證據力。
    """
    if score >= LOW_LEVEL_STRONG:
        return "🟢 強力抄底訊號", "#00cc88", "高度低估，分批進場／回補空單（需配合動態地板確認）"
    if score >= LOW_LEVEL_VALUE:
        return "🟢 明確低估", "#00aa66", "可開始定投／減空"
    if score >= LOW_LEVEL_COOL:
        return "🟡 偏冷觀察", "#ffcc00", "留意打底，勿純憑超賣搶反彈"
    if score >= LOW_LEVEL_NEUTRAL:
        return "⚪ 中性", "#9e9e9e", "正常持有"
    if score > LOW_VETO_VALIDATED:
        return "🔴 無底部訊號", "#ff4b4b", "無低估壓力，勿接刀"
    return ("⛔ 實證否決區", "#c62828",
            f"≤{LOW_VETO_VALIDATED} 分：其後180日中位 +1.3% vs 其他日 +27.5%，不進場")


def compute_relative_low(
    price: float, row, df: Optional[pd.DataFrame] = None, *,
    mvrv_z: Optional[float] = None, **kwargs,
) -> dict:
    """相對底部完整評估（評分 + 等級）。所有外部資料由呼叫端注入（本層零網路請求）。
    mvrv_z：2026-07 已驗證計入 low_score（onchain 子分，AUC 0.732）。"""
    score, signals = compute_relative_low_score(row, df, mvrv_z=mvrv_z, **kwargs)
    level, color, action = relative_low_meta(score)
    return {
        "low_score":   score,
        "low_level":   level,
        "low_color":   color,
        "low_action":  action,
        "low_signals": signals,
        "unfitted_dims": list(UNFITTED_DIMS_LOW),
        "rule_based_dims": list(RULE_BASED_DIMS_LOW),
    }
