"""
core/action_ensemble.py
三軸合成「單一行動建議」— 純函數，無 Streamlit 依賴，單一真實來源

把三個獨立量表合成一句可操作的行動：
  - 趨勢方向 trend_net ∈ [-100, +100]（風往哪吹，core/trend_direction）
  - 逃頂分數 escape ∈ [0, 100]（貴不貴・上行風險，core/relative_high）
  - 抄底分數 low ∈ [0, 100]（便不便宜・下行機會，core/relative_low）

決策矩陣依 CLAUDE.md 既有原則：順勢為主（趨勢軸優先分流）、
「空頭＋抄底高」勿純憑估值接刀、「強多＋逃頂高」分批止盈不加倉。

⚠️ 建議倉位區間為**專家設定（未擬合）**。2026-06 已用 radar_replay 回放嘗試擬合
   （tests/position_calib.py），結論「暫無法可靠擬合，維持專家階梯」：
     (1) 逃頂≥60/抄底≥75 的 estimation-gated 分支在回放保守下界（OI/ETF/SOPR=0，分數
         天花板 ~55）中幾乎不觸發、樣本不足；
     (2) 趨勢淨分 → 其後60日報酬呈 U 型（強空 +3.6% / 中性 -6.0% / 強多 +8.7%），非單調，
         無法簡單線性映射倉位（強空因均值回歸亦正報酬，但回撤風險高，不宜據此加倉）。
   現行階梯方向（強多倉位最高）與最佳報酬一致，故維持；待 OI/ETF/SOPR 歷史補齊（Phase 3）
   解除回放下界後重跑 position_calib 再校準。

⚠️ 2026-07-03 用台股代表性樣本重測同一個問題（`scripts/tw_position_calib.py`，系統抽樣
   200 檔、climber DB 2016 年起全歷史、每 5 個交易日取樣、out-of-sample≥2024），**結論同樣
   是「證據不夠、維持未擬合」**，不是 BTC 特例：
     用與本檔完全相同的呼叫方式（`compute_trend_score`→`compute_relative_high_tw`/
     `relative_low_tw`→本函式，不傳 `cycle_score`）重建歷史逐日 `action_key`，依 `pos_mid`
     排序分桶檢查後續 20/60 日報酬是否單調：
       IN-SAMPLE（n=55,454）桶級 pos_mid vs fwd60_mean 等級相關 +0.42（中等）；
       OUT-OF-SAMPLE（n=18,817）**+0.15（弱，大幅衰退，classic 樣本內過擬合警訊）**。
   根因與 BTC 版本一致：測試期（2016-2023 台股／BTC）皆為大牛市，市場貝塔蓋過分數排序的
   鑑別力，多數行動分類無論分數高低皆為正報酬，看不出真正排序訊號——**不是「台股資料不夠」
   （台股歷史資料很夠），是「絕對報酬在牛市期間本來就不太能拿來檢驗排序好壞」**，要驗證
   需改測「相對大盤超額報酬」排除貝塔，屬更大工程，尚未做。**勿再用同方法重測，除非改用
   超額報酬版重新設計。**

dashboard（tab_macro_compass）、LINE 推播（daily_line_notify/builders）共用本檔，
杜絕兩邊行動建議漂移。
"""
from typing import Optional

# 與 trend_meta / escape_top_meta / relative_low_meta 的分級邊界對齊
TREND_STRONG_BULL = 50   # trend_net ≥ 此值 → 強多頭（trend_meta）
TREND_BULL = 20      # trend_net ≥ 此值 → 多頭
TREND_BEAR = -20     # trend_net ≤ 此值 → 空頭
TREND_STRONG_BEAR = -50  # trend_net ≤ 此值 → 強空頭（trend_meta）
# 2026-08-25：原本硬編 ESCAPE_HOT=60 / LOW_STRONG=75 / LOW_VALUE=60，註解宣稱「與 meta
# 分級邊界對齊」但實際早已漂移 —— 而逃頂實測上限只有 55、抄底 65，
# 那三個值**永遠走不到**，等於 TAKE_PROFIT / REDUCE / BOTTOM_FISH 三個分支是死的。
# 這支的輸出是行動短語＋建議倉位，被 dashboard 與 LINE 推播消費，比分級文案嚴重得多。
# → 改成直接 import meta 的具名常數，**不要再抄一份數字**（抄一份就會再漂移一次）。
from core.relative_high import TOP_LEVEL_HOT, TOP_LEVEL_WARM       # noqa: E402
from core.relative_low import LOW_LEVEL_STRONG, LOW_LEVEL_VALUE    # noqa: E402

ESCAPE_HOT = TOP_LEVEL_HOT     # 逃頂明確過熱
ESCAPE_WARM = TOP_LEVEL_WARM   # 逃頂偏熱
LOW_STRONG = LOW_LEVEL_STRONG  # 強力抄底
LOW_VALUE = LOW_LEVEL_VALUE    # 明確低估
# 抄底 cycle 維度（長週期深跌，max 25，AUC 0.662）≥22 ≈ 跌破2年均×0.8「且」跌破200週均。
# 2 年回測歸納（scripts/backtest_composite.py）：≥18 太鬆會在 $79k 誤判低估；≥22 才對應真歷史
# 底部區（其後30d 平均 +15.4%）。即時版 low 常因 OI/ETF/SOPR 缺項被拉低，故以 cycle 直判補強。
CYCLE_DEEP_VALUE = 22

POSITION_NOTE = "倉位區間為專家設定（未擬合），僅供方向參考"  # 回放校準受限，見 tests/position_calib.py


def compute_composite_action(
    trend_net: Optional[float],
    escape_score: Optional[float],
    low_score: Optional[float],
    cycle_score: Optional[float] = None,
) -> Optional[dict]:
    """
    三軸 → 單一行動。trend_net 為 None 時無法分流，回 None（呼叫端隱藏該行）。
    escape/low 為 None 視為 0（與灰燈一致）。

    cycle_score（選填）：抄底 cycle 子分（0..25）。傳入且 ≥CYCLE_DEEP_VALUE 時，視同「明確低估」
      與 low≥LOW_VALUE 同級觸發 ADD/ACCUMULATE/WATCH_REVERSAL——補強即時版 low 因 OI/ETF/SOPR
      缺項被拉低時的底部辨識（不傳則行為與舊版完全相同，向後相容）。

    回傳 dict：
      action_key / emoji / action（短語）/ detail（一句話）/
      pos_low, pos_high（建議倉位 %）/ pos_label / color
    """
    if trend_net is None:
        return None
    esc = escape_score or 0
    low = low_score or 0
    # 長週期深跌（cycle≥22）視同「明確低估」，與 low≥LOW_VALUE 同級
    value = low >= LOW_VALUE or (cycle_score is not None and cycle_score >= CYCLE_DEEP_VALUE)

    if trend_net >= TREND_BULL:
        if esc >= ESCAPE_HOT:
            r = ("TAKE_PROFIT", "🟠", "分批止盈", "強多但明確過熱：上移止損、分批止盈，勿加倉",
                 30, 50, "#E67E22")
        elif esc >= ESCAPE_WARM:
            r = ("HOLD_TIGHTEN", "🟡", "續抱緊止盈", "多頭偏熱：續抱但預掛止盈、不追高",
                 50, 70, "#F39C12")
        elif value:
            r = ("ADD", "🟢", "回踩加倉", "多頭且仍低估：回踩均線分批加倉", 70, 100, "#27AE60")
        else:
            r = ("RIDE", "🟢", "順勢持有", "趨勢多頭、估值中性：持有為主，回踩找買點",
                 60, 80, "#27AE60")
    elif trend_net <= TREND_BEAR:
        if low >= LOW_STRONG:
            r = ("BOTTOM_FISH", "🟢", "小倉左側佈局", "空頭但強力低估：小倉分批佈局，嚴設止損"
                 "（左側交易，配合動態地板）", 20, 40, "#00AA66")
        elif value:
            r = ("WATCH_REVERSAL", "🟡", "觀望等右側", "空頭但長週期已到底部區：等趨勢轉正再進，勿純憑估值接刀",
                 10, 30, "#F39C12")
        elif esc >= ESCAPE_WARM:
            r = ("FADE_RALLY", "🔴", "反彈減碼", "空頭且反彈過熱：反彈減碼／偏空操作", 0, 20, "#E74C3C")
        else:
            r = ("DEFENSE", "🔴", "防守輕倉", "趨勢空頭：輕倉防守，趨勢未轉不搶反彈", 10, 30, "#E74C3C")
    else:
        if esc >= ESCAPE_HOT:
            r = ("REDUCE", "🟠", "減倉觀望", "盤整且過熱：減倉觀望，防假突破", 30, 50, "#E67E22")
        elif value:
            r = ("ACCUMULATE", "🟢", "區間下緣佈局", "盤整且低估：區間下緣分批佈局", 40, 70, "#27AE60")
        else:
            r = ("RANGE", "⚪", "區間操作", "無明確趨勢、估值中性：區間操作，等方向選擇", 40, 60, "#9E9E9E")

    key, emoji, action, detail, p_lo, p_hi, color = r
    return {
        "action_key": key, "emoji": emoji, "action": action, "detail": detail,
        "pos_low": p_lo, "pos_high": p_hi,
        "pos_label": f"建議倉位 {p_lo}–{p_hi}%（未擬合）",
        "color": color,
    }


def compute_trend_stance(trend_net: Optional[float], mom_label: Optional[str] = None) -> Optional[dict]:
    """
    股票/無衍生品標的用：趨勢方向（中長期）× 短線動能（這週，正交）→ 行動 dict。
    無逃頂/抄底/cycle 可用時的精簡版 composite；切點沿用 trend_meta 的 ±50/±20（不新增門檻），
    再用短線動能補「多頭回檔 / 空頭反彈」層次（比單看趨勢面板多一層）。

    trend_net：compute_trend_score 淨方向分（-100..+100）；None 回 None。
    mom_label：BTC_WATCH._short_momentum 回傳字串（含「偏多」/「偏空」/「中性」），選填。
    回傳 {action_key, emoji, action, detail, color}（無倉位區間，股票不給）。
    """
    if trend_net is None:
        return None
    up = bool(mom_label and "偏多" in mom_label)
    down = bool(mom_label and "偏空" in mom_label)

    if trend_net <= TREND_STRONG_BEAR:
        r = ("EXIT", "🔴", "減碼/出場", "強空頭趨勢——減碼/觀望，趨勢未轉不搶反彈", "#E74C3C")
    elif trend_net >= TREND_STRONG_BULL:
        r = ("RIDE_STRONG", "🟢", "順勢持有/加碼", "強多頭趨勢——順勢持有，回踩均線找加碼點", "#27AE60")
    elif trend_net >= TREND_BULL:
        r = (("PULLBACK", "🟡", "多頭回檔", "多頭趨勢但短線轉弱——持有/等回踩，勿追高", "#F39C12")
             if down else
             ("RIDE", "🟢", "順勢持有", "多頭趨勢——順勢持有", "#27AE60"))
    elif trend_net <= TREND_BEAR:
        r = (("BOUNCE", "🟡", "空頭反彈", "空頭趨勢中的短線反彈——勿追，反彈減碼", "#E67E22")
             if up else
             ("REDUCE", "🔴", "偏空減碼", "空頭趨勢——偏空操作/減碼", "#E67E22"))
    else:
        r = ("RANGE", "⚪", "區間觀望", "盤整無明確方向——區間操作，勿追突破", "#9E9E9E")

    key, emoji, action, detail, color = r
    return {"action_key": key, "emoji": emoji, "action": action, "detail": detail, "color": color}
