"""
core/momentum.py
時間序列動能（Time-Series Momentum, MOP 2012）— 純函數，無 Streamlit 依賴

出自「技術指標的真實用途」筆記唯一有硬學術背書的因子：
  Moskowitz, Ooi & Pedersen (2012, JFE) — 過去 1–12 個月報酬對下期報酬有預測力。

⚠️ **參考訊號，未計入加權評分**：MOP 2012 涵蓋股指/匯率/商品/債券期貨，**未含加密**，
   直接套 BTC 為外插；dashboard / watcher 僅「顯示」此讀數，不進 trend_direction 加權。

回測結論（`tests/momentum_backtest.py`，本地日線 2017–2026 跨週期，2026-07 執行，**負面結果**）：
  單一標的 BTC 的 TSM **無預測力、打不贏 Buy&Hold** →
    - 預測力：3/6/12M past-ret 對 30 日 forward-ret 的 AUC 全期 0.51/0.46/0.48、測試半 0.50/0.49/0.50
      （≈ 擲硬幣，全 <0.55）；命中率 ~48–52%。
    - 策略：pos=sign(past) 日頻多空，Sharpe 3M 0.44 / 6M 0.37（比 B&H 0.65 差，被翻倉+成本巴）、
      12M 0.64 ≈ B&H（等於長期做多，無 alpha）。
  根因：MOP 2012 的動能溢酬來自「**58 個資產分散**的趨勢跟隨」，靠跨資產分散；單一 BTC 無此結構。
  → **維持參考不計分**，除非日後有跨幣種/多資產框架再重測。勿再單獨把 BTC TSM 塞進加權。
"""
import math
from typing import Optional

# 月 ≈ 30 日（BTC 為 365 日市場）；3M / 6M / 12M
_LOOKBACKS = (90, 180, 365)
_LB_LABEL = {90: "3M", 180: "6M", 365: "12M"}


def time_series_momentum(df, lookbacks=_LOOKBACKS) -> dict:
    """過去 N 日報酬 → 動能傾向。回傳 {rets:{lb:ret}, n, n_pos, stance, label}。

    stance：'up'（全為正）/ 'down'（全為負）/ 'mixed'（分歧）/ None（資料不足）。
    """
    out = {"rets": {}, "n": 0, "n_pos": 0, "stance": None, "label": "⚪ 累積中"}
    if df is None or getattr(df, "empty", True) or "close" not in getattr(df, "columns", []):
        return out
    close = df["close"].dropna()
    rets = {}
    for lb in lookbacks:
        if len(close) > lb:
            base = float(close.iloc[-lb - 1])
            if base > 0:
                rets[lb] = float(close.iloc[-1]) / base - 1.0
    if not rets:
        return out
    n = len(rets)
    n_pos = sum(1 for r in rets.values() if r > 0)
    if n_pos == n:
        stance, lbl = "up", f"🟢 多頭動能（{n_pos}/{n} 期為正）"
    elif n_pos == 0:
        stance, lbl = "down", f"🔴 空頭動能（0/{n} 期為正）"
    else:
        stance, lbl = "mixed", f"🟡 動能分歧（{n_pos}/{n} 期為正）"
    out.update({"rets": rets, "n": n, "n_pos": n_pos, "stance": stance, "label": lbl})
    return out


def momentum_ref_rows(df) -> list:
    """TSM 參考訊號顯示列。回傳 0–1 列。

    ⚠️ 標籤寫「已回測無效」而非「待回測」：`tests/momentum_backtest.py` 2026-07 已跑過
    （見檔頭），結論是負面（無預測力、打不贏 B&H），不是「還沒測」。維持顯示僅供參考、
    不計分是**已下的結論**，寫成待回測會誤導成「還沒驗證」。"""
    m = time_series_momentum(df)
    if not m["rets"]:
        return []
    parts = "  ".join(f"{_LB_LABEL.get(lb, f'{lb}d')} {r * 100:+.0f}%"
                      for lb, r in m["rets"].items())
    return [f"  〔動能·參考·已回測無效〕 {parts} → {m['label']}"]
