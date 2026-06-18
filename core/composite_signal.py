"""
core/composite_signal.py  ·  v1.0
三軸融合操作訊號 — 單一真實來源，純函數、零網路請求。

逃頂（貴不貴）、抄底（便宜不便宜）、趨勢方向（風往哪吹）三軸是正交量表；單看任一軸都會漏判
（實證：2026-05 $82k→$59k 那波，逃頂全程低分「沒過熱」，真正示警的是趨勢軸 5/22 翻強空頭）。
本模組把三軸**用各軸既有、已驗證的等級切點**組成狀態機，輸出一個可操作的綜合 stance，
**不新增門檻擬合**（切點全部沿用 trend_meta ±50/±20、escape_top_meta/relative_low_meta 75/60/45、
cycle 維度 AUC 0.662 子分），避免拿少數轉折點過擬合。

狀態（優先序，先命中先回傳）：
  1. 🟡 估值到底·等止穩  深跌且趨勢仍強空 → 便宜但風還在下吹，勿接刀
  2. 🟢 分批進場          深跌且趨勢脫離強空 → 便宜＋風停止下吹
  3. 🔴 減碼/出場         逃頂過熱 或 趨勢翻強空 → 出貨/趨勢破位
  4. 🟢 順勢持有          趨勢多頭且未過熱
  5. ⚪ 觀望              其餘（盤整/無明確訊號）
"""
from typing import Tuple

# 切點（全部沿用各軸既有等級，非本模組新增）
TREND_STRONG_BEAR = -50   # trend_meta：≤-50 為強空頭趨勢
TREND_BULL = 20           # trend_meta：≥+20 為多頭趨勢
TOP_OVERHEAT = 60         # escape_top_meta：≥60「明確過熱」
LOW_UNDERVALUED = 60      # relative_low_meta：≥60「明確低估」
CYCLE_DEEP_VALUE = 22     # 抄底 cycle 維度（max 25，AUC 0.662）≥22 ≈ 跌破2年均×0.8「且」跌破200週均
#                           （2 年回測歸納：≥18 太鬆，會在 $79k/趨勢未轉空時誤亮買訊→其後 -25%；
#                            ≥22 才對應真歷史底部區，其後 30d 平均 +11.5%。見 scripts/backtest_composite.py）


def compute_composite_signal(
    trend_net: int, top_score: int, low_score: int, cycle_score: int,
) -> Tuple[str, str, str, str]:
    """
    三軸 → (signal_key, 等級標籤, 顏色, 操作意涵)。

    trend_net  : core.trend_direction.compute_trend_score 的淨方向分（-100..+100）
    top_score  : core.relative_high.compute_escape_top_score 的逃頂分（0..100）
    low_score  : core.relative_low.compute_relative_low_score 的抄底分（0..100）
    cycle_score: 抄底 signals['cycle']['score']（長週期深跌維度，0..25，最強且純價格可重建）
    """
    deep_value = cycle_score >= CYCLE_DEEP_VALUE or low_score >= LOW_UNDERVALUED
    strong_bear = trend_net <= TREND_STRONG_BEAR

    if deep_value and strong_bear:
        return ("value_wait", "🟡 估值到底·等止穩", "#ffcc00",
                "長週期估值已到底部區，但趨勢仍強空頭——勿純憑便宜接刀，等趨勢脫離強空再進")
    if deep_value:
        return ("buy", "🟢 分批進場", "#00cc88",
                "估值便宜＋趨勢已脫離強空（風停止下吹）——可分批進場/回補")
    if top_score >= TOP_OVERHEAT or strong_bear:
        basis = "逃頂過熱出貨" if top_score >= TOP_OVERHEAT else "趨勢翻強空頭破位"
        return ("exit", "🔴 減碼/出場", "#ff4b4b",
                f"{basis}——減碼/止盈或觀望，趨勢未轉不搶反彈")
    if trend_net >= TREND_BULL and top_score < 45:
        return ("hold_long", "🟢 順勢持有", "#00aa66",
                "趨勢多頭且未過熱——順勢持有，回踩均線找加碼點")
    return ("neutral", "⚪ 觀望", "#9e9e9e",
            "無明確訊號（盤整/估值與方向皆中性）——區間操作，勿追突破")
