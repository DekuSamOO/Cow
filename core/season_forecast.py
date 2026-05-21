"""
core/season_forecast.py  ·  v1.3
四季理論目標價預測系統
─────────────────────────────────────────────────────────────────────
版次記錄:
  v1.0  初版，純時間季節（減半後月份判斷）
  v1.1  修正 add_vline 字串 x 座標 TypeError
  v1.2  新增市場狀態校正層（analyze_market_state / _derive_real_season）
  v1.3  [本次] 修正以下問題：
        ① analyze_market_state: df.index tz-aware vs naive datetime 比較錯誤
          → mask_cycle / mask_prev 全部改用 tz 標準化後比較
          → 導致 cycle_ath 取到全 df max（偏高），熊市目標價算錯
        ② forecast_price: prev_ath 也加 tz 標準化保護
        ③ CYCLE_HISTORY: 新增第4週期已知數據（ATH=$108,268，2025-01-20）
          標記 is_complete=False，F4 表格顯示「進行中」而非「預測」
        ④ 熊市卡片標籤：改為「最深目標/中位數目標/最淺目標」消除歧義
        ⑤ get_cycle_comparison_table: 第4週期顯示已知ATH + 狀態標記
        ⑥ F3 圖說新增冪律模型說明 README

冪律模型說明:
  公式：Price = 10^(-17.01467 + 5.84 × log10(days_since_genesis))
  來源：Giovanni Santostasi「比特幣冪律理論」
  用途：長期公允價值估算，非短期目標價
  走廊：±0.45 log10 = 約 ±2.8 倍（含蓋歷史 95% 以上的日線收盤）
  重要：冪律模型是長期趨勢，不代表短期會到達該價格
        熊市場景下，實際價格可能大幅低於冪律公允價值

歷史減半日:
  Halving 1: 2012-11-28
  Halving 2: 2016-07-09
  Halving 3: 2020-05-11
  Halving 4: 2024-04-19  ← 已發生，ATH $108,268 (2025-01-20，進行中)
  Halving 5: ~2028-04-17 (預估)

純 Python，無 Streamlit 依賴
"""

from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Any, cast, List, Dict
import numpy as np
import pandas as pd


HALVING_DATES = [
    datetime(2012, 11, 28),
    datetime(2016, 7,   9),
    datetime(2020, 5,  11),
    datetime(2024, 4,  19),
    datetime(2028, 4,  17),
]

CYCLE_HISTORY = [
    {
        "halving":       datetime(2012, 11, 28),
        "halving_price": 12.35,
        "ath_price":     1163.0,
        "ath_date":      datetime(2013, 11, 29),
        "bear_low":      152.40,
        "bear_low_date": datetime(2015, 1, 14),
        "peak_mult":     94.2,
        "bottom_mult":   0.131,   # 152.40 / 1163.0
        "peak_days":     366,
        "bottom_days":   777,
        "is_complete":   True,
    },
    {
        "halving":       datetime(2016, 7, 9),
        "halving_price": 650.0,
        "ath_price":     19891.0,
        "ath_date":      datetime(2017, 12, 17),
        "bear_low":      3122.0,
        "bear_low_date": datetime(2018, 12, 15),
        "peak_mult":     30.6,
        "bottom_mult":   0.157,   # 3122 / 19891
        "peak_days":     526,
        "bottom_days":   889,
        "is_complete":   True,
    },
    {
        "halving":       datetime(2020, 5, 11),
        "halving_price": 8571.0,
        "ath_price":     68789.0,
        "ath_date":      datetime(2021, 11, 10),
        "bear_low":      15476.0,
        "bear_low_date": datetime(2022, 11, 21),
        "peak_mult":     8.03,
        "bottom_mult":   0.225,   # 15476 / 68789
        "peak_days":     549,
        "bottom_days":   925,
        "is_complete":   True,
    },
    {
        # ── 第4週期：已知部分數據（ATH 已發生，熊市底部尚未完成）──
        "halving":       datetime(2024, 4, 19),
        "halving_price": 63842.0,           # 2024-04-19 收盤
        "ath_price":     108268.0,           # 2025-01-20 收盤（已發生）
        "ath_date":      datetime(2025, 1, 20),
        "bear_low":      None,               # 尚未完成
        "bear_low_date": None,
        "peak_mult":     1.70,               # 108268 / 63842（已知）
        "bottom_mult":   None,               # 尚未完成
        "peak_days":     276,                # 2024-04-19 → 2025-01-20
        "bottom_days":   None,               # 尚未完成
        "is_complete":   False,              # 進行中
    },
]

# ── 只取已完成週期計算統計 ──────────────────────────────────────────
_completed = [c for c in CYCLE_HISTORY if c["is_complete"]]
_peak_mults       = [c["peak_mult"]    for c in _completed]
_bottom_mults     = [c["bottom_mult"]  for c in _completed]
_peak_days_list   = [c["peak_days"]    for c in _completed]
_bottom_days_list = [c["bottom_days"]  for c in _completed]

STATS = {
    "peak_mult_median":   float(np.exp(np.median(np.log(_peak_mults)))),
    "peak_mult_p25":      float(np.exp(np.percentile(np.log(_peak_mults), 25))),
    "peak_mult_p75":      float(np.exp(np.percentile(np.log(_peak_mults), 75))),
    "bottom_mult_median": float(np.median(_bottom_mults)),
    "bottom_mult_p25":    float(np.percentile(_bottom_mults, 25)),  # 跌更深（悲觀）
    "bottom_mult_p75":    float(np.percentile(_bottom_mults, 75)),  # 跌較淺（樂觀）
    "peak_days_median":   int(np.median(_peak_days_list)),
    "bottom_days_median": int(np.median(_bottom_days_list)),
}


def _tz_safe_timestamp(dt: datetime) -> pd.Timestamp:
    """
    將 naive datetime 轉為 UTC-aware pd.Timestamp，
    避免與 tz-aware df.index 比較時靜默全 True 或拋出 TypeError。
    """
    if dt.tzinfo is None:
        return pd.Timestamp(dt, tz="UTC")
    return pd.Timestamp(dt)


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """若 df.index 有 timezone，去除後回傳 copy（保持 naive DatetimeIndex）。"""
    if df.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df


def analyze_market_state(current_price: float, df: pd.DataFrame, current_halving: datetime):
    """
    分析真實市場狀態。

    [v1.3 修正] df.index tz 標準化，避免 naive vs tz-aware 比較錯誤。

    返回 dict:
      cycle_ath         : 當前週期（減半後）最高收盤價
      cycle_ath_date    : ATH 日期
      drawdown_from_ath : 從 ATH 跌幅（負值，-0.18 = 跌 18%）
      sma200            : 200 日均線
      price_vs_sma200   : current_price / sma200
      is_above_sma200   : 是否在年線上方
    """
    result = {
        "cycle_ath":         current_price,
        "cycle_ath_date":    datetime.utcnow(),
        "drawdown_from_ath": 0.0,
        "price_vs_sma200":   1.0,
        "sma200":            current_price,
        "is_above_sma200":   True,
    }

    if df is None or df.empty or "close" not in df.columns:
        return result

    # ▸ 統一去除 timezone，避免比較失敗
    df_naive = _strip_tz(df)
    halving_ts = pd.Timestamp(current_halving)  # naive

    mask_cycle = df_naive.index >= halving_ts
    if mask_cycle.any():
        cycle_data   = df_naive.loc[mask_cycle, "close"]
        cycle_ath    = float(cycle_data.max())
        cycle_ath_dt = cycle_data.idxmax().to_pydatetime()
        result["cycle_ath"]      = cycle_ath
        result["cycle_ath_date"] = cycle_ath_dt

    result["drawdown_from_ath"] = (current_price - result["cycle_ath"]) / result["cycle_ath"]

    sma200 = (float(df_naive["close"].rolling(200).mean().iloc[-1])
              if len(df_naive) >= 200
              else float(df_naive["close"].mean()))
    result["sma200"]          = sma200
    result["price_vs_sma200"] = current_price / sma200 if sma200 > 0 else 1.0
    result["is_above_sma200"] = current_price > sma200

    return result


def _derive_real_season(time_season, drawdown, is_above_sma200, month_in_cycle):
    """
    根據真實市場狀態推導有效季節。
    返回: (real_season, real_season_zh, real_emoji, correction_reason, is_corrected)
    """
    if drawdown < -0.30 and not is_above_sma200:
        reason = (f"⚠️ 市場校正：從當前週期 ATH 跌幅 {abs(drawdown)*100:.1f}%，"
                  f"已跌破年線，實際處於深熊（冬季）。時間季節（{time_season}）僅供參考。")
        return "winter", "冬季 — 深熊底部", "❄️", reason, time_season not in ("autumn", "winter")

    if drawdown < -0.20 and not is_above_sma200:
        reason = (f"⚠️ 市場校正：從當前週期 ATH 跌幅 {abs(drawdown)*100:.1f}%，"
                  f"已跌破年線，實際處於熊市初期（秋季）。時間季節（{time_season}）僅供參考。")
        return "autumn", "秋季 — 熊市初期", "🍂", reason, time_season not in ("autumn", "winter")

    if drawdown < -0.15 and not is_above_sma200 and time_season in ("spring", "summer"):
        reason = (f"⚠️ 市場校正：時間位置為{time_season}（月{month_in_cycle}），"
                  f"但跌幅 {abs(drawdown)*100:.1f}% 且跌破年線，提前進入秋季修正。")
        return "autumn", "秋季 — 提前入秋", "🍂", reason, True

    if drawdown < -0.10 and not is_above_sma200 and time_season in ("spring", "summer"):
        reason = (f"⚠️ 市場警示：跌幅 {abs(drawdown)*100:.1f}% 且跌破年線，"
                  f"牛市動能受阻，以秋季修正視角預測。")
        return "autumn", "秋季 — 牛市受阻", "🍂", reason, True

    label_map = {
        "spring": ("春季 — 復甦期",   "🌱"),
        "summer": ("夏季 — 牛市高峰", "☀️"),
        "autumn": ("秋季 — 泡沫破裂", "🍂"),
        "winter": ("冬季 — 熊市底部", "❄️"),
    }
    s_zh, emoji = label_map.get(time_season, ("未知", "❓"))
    return time_season, s_zh, emoji, None, False


def get_current_season(as_of: Optional[datetime] = None):
    """計算「時間季節」（純減半週期時間位置，不含市場校正）。"""
    if as_of is None:
        as_of = datetime.utcnow()

    past_halvings = [h for h in HALVING_DATES if h <= as_of]
    if not past_halvings:
        return None
    current_halving = past_halvings[-1]

    future_halvings = [h for h in HALVING_DATES if h > as_of]
    next_halving    = future_halvings[0] if future_halvings else current_halving + timedelta(days=1460)

    days_since     = (as_of - current_halving).days
    days_total     = (next_halving - current_halving).days
    days_to_next   = (next_halving - as_of).days
    month_in_cycle = int(days_since / 30.44)
    cycle_progress = min(days_since / days_total, 1.0)

    if month_in_cycle < 12:
        season, season_zh, emoji = "spring", "春季 — 復甦期", "🌱"
    elif month_in_cycle < 24:
        season, season_zh, emoji = "summer", "夏季 — 牛市高峰", "☀️"
    elif month_in_cycle < 36:
        season, season_zh, emoji = "autumn", "秋季 — 泡沫破裂", "🍂"
    else:
        season, season_zh, emoji = "winter", "冬季 — 熊市底部", "❄️"

    return {
        "season":         season,
        "season_zh":      season_zh,
        "emoji":          emoji,
        "halving_date":   current_halving,
        "next_halving":   next_halving,
        "days_since":     days_since,
        "days_to_next":   days_to_next,
        "cycle_progress": cycle_progress,
        "month_in_cycle": month_in_cycle,
    }


def _apply_diminishing_returns(base_mult: float, cycle_index: int) -> float:
    """每個週期牛市漲幅遞減約 3.5 倍，以最後一個完成週期為基準外插。"""
    diminish_factor = 3.5
    ref_cycle = len(_completed) - 1  # 最後一個完成週期 index
    delta = cycle_index - ref_cycle
    if delta <= 0:
        return base_mult
    return base_mult / (diminish_factor ** delta)


def forecast_price(current_price: float, df: Optional[pd.DataFrame] = None, as_of: Optional[datetime] = None):
    """
    主要預測函數。整合時間季節 + 真實市場狀態，預測未來12個月目標價。

    [v1.3] 修正：
    - df tz 標準化移至 analyze_market_state（統一處理）
    - prev_ath 計算也加 tz 標準化保護
    - 熊市標籤改為：deepest（最深）/ median（中位數）/ shallowest（最淺）

    返回 dict 額外欄位（v1.3 新增）:
      bear_label_low    : 熊市三標籤（「最深目標」等）
      bear_label_mid    : 熊市中間標籤
      bear_label_high   : 熊市最淺標籤
    """
    if as_of is None:
        as_of = datetime.utcnow()

    season_info = get_current_season(as_of)
    if season_info is None:
        return None

    current_halving   = season_info["halving_date"]
    current_cycle_idx = HALVING_DATES.index(current_halving)

    halving_price = current_price
    prev_ath      = None

    if df is not None and not df.empty and "close" in df.columns:
        df_naive = _strip_tz(df)  # ▸ tz 標準化
        halving_ts = pd.Timestamp(current_halving)

        halving_mask = df_naive.index >= halving_ts
        if halving_mask.any():
            halving_price = float(df_naive.loc[halving_mask, "close"].iloc[0])

        if current_cycle_idx > 0:
            prev_halving = HALVING_DATES[current_cycle_idx - 1]
            mask_prev    = (df_naive.index >= pd.Timestamp(prev_halving)) & \
                           (df_naive.index <  halving_ts)
            if mask_prev.any():
                prev_ath = float(df_naive.loc[mask_prev, "close"].max())

    # 已知第4週期 ATH 備援：若 prev_ath 取到的是前一週期，但當前週期已有更高的 ATH
    # cycle_history 中若有當前週期的已知 ATH，優先採用
    known_cycle = next((c for c in CYCLE_HISTORY if c["halving"] == current_halving), None)
    known_cycle_ath = known_cycle["ath_price"] if known_cycle and known_cycle["ath_price"] else None

    if prev_ath is None:
        prev_ath = CYCLE_HISTORY[-2]["ath_price"] if len(CYCLE_HISTORY) >= 2 else 68789.0

    market_state = analyze_market_state(current_price, df, current_halving)

    real_season, real_season_zh, real_emoji, correction_reason, is_corrected = _derive_real_season(
        time_season     = season_info["season"],
        drawdown        = market_state["drawdown_from_ath"],
        is_above_sma200 = market_state["is_above_sma200"],
        month_in_cycle  = season_info["month_in_cycle"],
    )

    effective_season = {
        "season":    real_season,
        "season_zh": real_season_zh,
        "emoji":     real_emoji,
    }

    adj_peak_med = _apply_diminishing_returns(STATS["peak_mult_median"], current_cycle_idx)
    adj_peak_p25 = _apply_diminishing_returns(STATS["peak_mult_p25"],    current_cycle_idx)
    adj_peak_p75 = _apply_diminishing_returns(STATS["peak_mult_p75"],    current_cycle_idx)

    days_since = season_info["days_since"]

    if real_season in ("spring", "summer"):
        # ═══ 牛市預測 ═══
        forecast_type = "bull_peak"

        ath_target_med = halving_price * adj_peak_med
        ath_target_p25 = halving_price * adj_peak_p25
        ath_target_p75 = halving_price * adj_peak_p75

        if current_price > ath_target_med:
            remaining_mult = adj_peak_p75 / adj_peak_med
            ath_target_med = current_price * remaining_mult
            ath_target_p75 = ath_target_med * 1.3
            ath_target_p25 = ath_target_med * 0.75

        target_median = max(ath_target_med, current_price)
        target_low    = max(ath_target_p25, current_price)   # 牛市：低 = 保守 = 漲幅小
        target_high   = max(ath_target_p75, current_price)   # 牛市：高 = 樂觀 = 漲幅大

        days_to_peak   = max(STATS["peak_days_median"] - days_since, 30)
        estimated_date = as_of + timedelta(days=days_to_peak)

        rationale = (
            f"【有效季節】{real_emoji} {real_season_zh}\n"
            f"時間位置：第 {current_cycle_idx+1} 次減半後第 {season_info['month_in_cycle']} 個月\n"
            f"歷史中位數：減半後約 {STATS['peak_days_median']} 天達牛市高點，"
            f"相對減半價漲幅中位數 {adj_peak_med:.1f}x\n"
            f"減半時價格: ${halving_price:,.0f}\n"
            f"預計牛市高點區間: ${target_low:,.0f} ~ ${target_high:,.0f}"
        )

        confidence = min(int(80 - abs(days_since - STATS["peak_days_median"]) / 5), 85)
        confidence = max(confidence, 40)
        if market_state["drawdown_from_ath"] < -0.10:
            confidence = max(confidence - 15, 25)

        bear_label_low  = "保守目標（漲幅較小）"
        bear_label_mid  = "中位數目標"
        bear_label_high = "樂觀目標（漲幅較大）"

    else:
        # ═══ 熊市預測 ═══
        forecast_type = "bear_bottom"

        # ▸ 取「CYCLE_HISTORY 寫死值」與「df 實算 cycle_ath」兩者較大者
        #   原本順序是先 CYCLE_HISTORY 再 df → 寫死值過時時會偏低（如本輪 2025-10-06
        #   創高後，CYCLE_HISTORY 仍停在 2025-01-20 的 $108,268）。
        # ▸ 兩者皆不顯著高於現價時，退回前一週期 ATH。
        cycle_ath_ms = market_state.get("cycle_ath", 0)
        cycle_candidates = [v for v in (known_cycle_ath, cycle_ath_ms) if v]
        best_cycle_ath = max(cycle_candidates) if cycle_candidates else 0

        if best_cycle_ath and best_cycle_ath > current_price * 1.05:
            ath_ref = best_cycle_ath
            if cycle_ath_ms and cycle_ath_ms >= (known_cycle_ath or 0):
                ath_ref_label = f"當前週期實算 ATH ${cycle_ath_ms:,.0f}"
            else:
                ath_ref_label = f"當前週期已知 ATH ${known_cycle_ath:,.0f}"
        else:
            ath_ref       = prev_ath
            ath_ref_label = f"前一週期 ATH ${prev_ath:,.0f}（當前週期ATH尚不明確）"

        bottom_med = ath_ref * STATS["bottom_mult_median"]  # 中位數底部
        bottom_p25 = ath_ref * STATS["bottom_mult_p25"]     # 跌更深（悲觀）
        bottom_p75 = ath_ref * STATS["bottom_mult_p75"]     # 跌較淺（樂觀）

        # 熊市：min 截斷（底部不可能高於現價）
        target_median = min(bottom_med, current_price)
        target_low    = min(bottom_p25, current_price)   # 熊市：low = 最悲觀 = 跌最深
        target_high   = min(bottom_p75, current_price)   # 熊市：high = 最樂觀 = 跌最淺

        days_to_bottom = max(STATS["bottom_days_median"] - days_since, 30)
        estimated_date = as_of + timedelta(days=days_to_bottom)

        drawdown_pct = abs(market_state["drawdown_from_ath"]) * 100
        rationale = (
            f"【有效季節】{real_emoji} {real_season_zh}\n"
            f"時間位置：第 {current_cycle_idx+1} 次減半後第 {season_info['month_in_cycle']} 個月 "
            f"（時間季節：{season_info['season_zh']}）\n"
            f"距 ATH 跌幅: {drawdown_pct:.1f}%  |  "
            f"{'跌破' if not market_state['is_above_sma200'] else '站上'} 200日均線 "
            f"(${market_state['sma200']:,.0f})\n"
            f"參考基準: {ath_ref_label}\n"
            f"歷史底部跌幅中位數 {(1-STATS['bottom_mult_median'])*100:.0f}%（從ATH計）\n"
            f"預計熊市底部區間: ${target_low:,.0f} ~ ${target_high:,.0f}"
        )

        confidence = min(int(80 - abs(days_since - STATS["bottom_days_median"]) / 5), 80)
        confidence = max(confidence, 35)
        if market_state["drawdown_from_ath"] < -0.25:
            confidence = min(confidence + 10, 75)

        # ▸ v1.3: 熊市標籤改為方向明確的描述
        bear_label_low  = "最深目標（歷史最大跌幅）"
        bear_label_mid  = "中位數目標（歷史平均）"
        bear_label_high = "最淺目標（最輕微熊市）"

    return {
        "season_info":         season_info,
        "market_state":        market_state,
        "effective_season":    effective_season,
        "forecast_type":       forecast_type,
        "target_median":       round(target_median, 0),
        "target_low":          round(target_low,    0),
        "target_high":         round(target_high,   0),
        "estimated_date":      estimated_date,
        "rationale":           rationale,
        "confidence":          confidence,
        "current_cycle_idx":   current_cycle_idx,
        "halving_price":       round(halving_price, 0),
        "prev_ath":            round(prev_ath, 0) if prev_ath else None,
        "is_season_corrected": is_corrected,
        "correction_reason":   correction_reason,
        "bear_label_low":      bear_label_low,
        "bear_label_mid":      bear_label_mid,
        "bear_label_high":     bear_label_high,
        "ath_ref":             round(ath_ref, 0) if forecast_type == "bear_bottom" else None,
    }


def get_cycle_comparison_table():
    """
    返回歷史各週期比較表 (pd.DataFrame)。
    [v1.3] 第4週期標「進行中」，顯示已知ATH，底部欄位顯示「尚未完成」。
    """
    rows = []
    for i, c in enumerate(CYCLE_HISTORY):
        if c["is_complete"]:
            rows.append({
                "週期":        f"第 {i+1} 次減半",
                "狀態":        "✅ 完成",
                "減半日":      c["halving"].strftime("%Y-%m-%d"),
                "減半時價格":  f"${c['halving_price']:,.0f}",
                "牛市 ATH":    f"${c['ath_price']:,.0f}",
                "ATH 倍數":    f"{c['peak_mult']:.1f}x",
                "達 ATH 天數": f"{c['peak_days']} 天",
                "熊市最低點":  f"${c['bear_low']:,.0f}",
                "ATH 跌幅":    f"{(1-c['bottom_mult'])*100:.0f}%",
                "達底部天數":  f"{c['bottom_days']} 天",
            })
        else:
            rows.append({
                "週期":        f"第 {i+1} 次減半",
                "狀態":        "🔄 進行中",
                "減半日":      c["halving"].strftime("%Y-%m-%d"),
                "減半時價格":  f"${c['halving_price']:,.0f}",
                "牛市 ATH":    f"${c['ath_price']:,.0f} ✓",
                "ATH 倍數":    f"{c['peak_mult']:.2f}x",
                "達 ATH 天數": f"{c['peak_days']} 天",
                "熊市最低點":  "—（尚未完成）",
                "ATH 跌幅":    "—",
                "達底部天數":  "—",
            })
    return pd.DataFrame(rows)


def get_power_law_forecast(df: pd.DataFrame, months_ahead: int = 12):
    """
    冪律模型：未來 months_ahead 個月的長期公允價值走廊。

    公式: Price = 10^(-17.01467 + 5.84 × log10(days_since_genesis))
    來源: Giovanni Santostasi 比特幣冪律理論
    說明: 此為長期趨勢估值，非短期目標，不代表短期會到達該價位。
          ±0.45 對數通道含蓋歷史 95%+ 日線收盤，僅供長期參考。
    """
    genesis      = datetime(2009, 1, 3)
    future_dates = pd.date_range(
        start   = datetime.utcnow() + timedelta(days=1),
        periods = months_ahead * 30,
        freq    = "D",
    )
    days_arr   = np.array([(d.to_pydatetime() - genesis).days for d in future_dates], dtype=float)
    days_arr   = np.clip(days_arr, 1, None)
    log_median = -17.01467 + 5.84 * np.log10(days_arr)

    return pd.DataFrame({
        "median": 10 ** log_median,
        "upper":  10 ** (log_median + 0.45),
        "lower":  10 ** (log_median - 0.45),
    }, index=future_dates)