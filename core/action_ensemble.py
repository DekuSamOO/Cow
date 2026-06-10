"""
core/action_ensemble.py
三軸合成「單一行動建議」— 純函數，無 Streamlit 依賴，單一真實來源

把三個獨立量表合成一句可操作的行動：
  - 趨勢方向 trend_net ∈ [-100, +100]（風往哪吹，core/trend_direction）
  - 逃頂分數 escape ∈ [0, 100]（貴不貴・上行風險，core/relative_high）
  - 抄底分數 low ∈ [0, 100]（便不便宜・下行機會，core/relative_low）

決策矩陣依 CLAUDE.md 既有原則：順勢為主（趨勢軸優先分流）、
「空頭＋抄底高」勿純憑估值接刀、「強多＋逃頂高」分批止盈不加倉。

⚠️ 建議倉位區間為**專家設定（未擬合）**：待雷達歷史回放（core/radar_replay）
   累積驗證後再校準，介面需標示。

dashboard（tab_macro_compass）、LINE 推播（daily_line_notify/builders）共用本檔，
杜絕兩邊行動建議漂移。
"""
from typing import Optional

# 與 trend_meta / escape_top_meta / relative_low_meta 的分級邊界對齊
TREND_BULL = 20      # trend_net ≥ 此值 → 多頭
TREND_BEAR = -20     # trend_net ≤ 此值 → 空頭
ESCAPE_HOT = 60      # 逃頂明確過熱（= LINE 警報門檻）
ESCAPE_WARM = 45     # 逃頂偏熱
LOW_STRONG = 75      # 強力抄底
LOW_VALUE = 60       # 明確低估

POSITION_NOTE = "倉位區間為專家設定（未擬合），僅供方向參考"


def compute_composite_action(
    trend_net: Optional[float],
    escape_score: Optional[float],
    low_score: Optional[float],
) -> Optional[dict]:
    """
    三軸 → 單一行動。trend_net 為 None 時無法分流，回 None（呼叫端隱藏該行）。
    escape/low 為 None 視為 0（與灰燈一致）。

    回傳 dict：
      action_key / emoji / action（短語）/ detail（一句話）/
      pos_low, pos_high（建議倉位 %）/ pos_label / color
    """
    if trend_net is None:
        return None
    esc = escape_score or 0
    low = low_score or 0

    if trend_net >= TREND_BULL:
        if esc >= ESCAPE_HOT:
            r = ("TAKE_PROFIT", "🟠", "分批止盈", "強多但明確過熱：上移止損、分批止盈，勿加倉",
                 30, 50, "#E67E22")
        elif esc >= ESCAPE_WARM:
            r = ("HOLD_TIGHTEN", "🟡", "續抱緊止盈", "多頭偏熱：續抱但預掛止盈、不追高",
                 50, 70, "#F39C12")
        elif low >= LOW_VALUE:
            r = ("ADD", "🟢", "回踩加倉", "多頭且仍低估：回踩均線分批加倉", 70, 100, "#27AE60")
        else:
            r = ("RIDE", "🟢", "順勢持有", "趨勢多頭、估值中性：持有為主，回踩找買點",
                 60, 80, "#27AE60")
    elif trend_net <= TREND_BEAR:
        if low >= LOW_STRONG:
            r = ("BOTTOM_FISH", "🟢", "小倉左側佈局", "空頭但強力低估：小倉分批佈局，嚴設止損"
                 "（左側交易，配合動態地板）", 20, 40, "#00AA66")
        elif low >= LOW_VALUE:
            r = ("WATCH_REVERSAL", "🟡", "觀望等右側", "空頭但已低估：等趨勢轉正再進，勿純憑估值接刀",
                 10, 30, "#F39C12")
        elif esc >= ESCAPE_WARM:
            r = ("FADE_RALLY", "🔴", "反彈減碼", "空頭且反彈過熱：反彈減碼／偏空操作", 0, 20, "#E74C3C")
        else:
            r = ("DEFENSE", "🔴", "防守輕倉", "趨勢空頭：輕倉防守，趨勢未轉不搶反彈", 10, 30, "#E74C3C")
    else:
        if esc >= ESCAPE_HOT:
            r = ("REDUCE", "🟠", "減倉觀望", "盤整且過熱：減倉觀望，防假突破", 30, 50, "#E67E22")
        elif low >= LOW_VALUE:
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
