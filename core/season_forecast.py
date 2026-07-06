"""
core/season_forecast.py  ·  v1.4
四季理論目標價預測系統
─────────────────────────────────────────────────────────────────────
版次記錄:
  v1.6  [A1] 牛市側以「當前週期已知 peak_mult」校正等比遞減外推（_current_cycle_known_peak_mult）。
        第4輪等比外推 8.74x 但實際已知 ATH 僅 1.70x（高估 5 倍）；對 ATH 已印出的當前週期用已知值
        重錨、保留 p25/p75 band 比例，未來尚無 ATH 的週期維持外插不變。熊底側不動（n=3 硬限制，
        現況線性外插+固定 band 已是合理小樣本防護）。
  v1.4  熊底 bottom_mult 改「週期趨勢外插」(extrapolate_bottom_mult) 取代 median/p25——
        bottom_mult 三輪 0.131→0.157→0.225 單調遞增（底部漸淺），舊 median 把當前輪
        預測過深。線性迴歸外插（留一法誤差 -19% 優於 median -30%）。與峰值側
        _apply_diminishing_returns 對稱。bottom_floors.py 再以礦工電費/on-chain 錨地板化。
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
  Halving 4: 2024-04-19  ← 已發生，ATH $124,658.54 (2025-10-06 收盤，進行中；
             T-20 2026-07-06 修正，原記 $108,268 為 2025-01 前段高點)
  Halving 5: ~2028-04-17 (預估)

純 Python，無 Streamlit 依賴
"""

from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Any, cast, List, Dict
import numpy as np
import pandas as pd


def _utcnow_naive() -> datetime:
    """UTC 現在時刻的 naive datetime。本檔全程以 naive 與 HALVING_DATES 比較，
    故統一去 tz（取代 Python 3.12 已棄用的 datetime.utcnow()）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        "ath_price":     124658.54,          # 2025-10-06 收盤（T-20 修正：原 108,268 為
                                             # 2025-01 前段高點，市場其後創新高；本值須隨
                                             # 新高更新——live 路徑有 df 實算 max 自癒，
                                             # 但本常數是 df 缺失時的 fallback 與人工分析依據）
        "ath_date":      datetime(2025, 10, 6),
        "bear_low":      None,               # 尚未完成
        "bear_low_date": None,
        "peak_mult":     1.95,               # 124658.54 / 63842（已知）
        "bottom_mult":   None,               # 尚未完成
        "peak_days":     535,                # 2024-04-19 → 2025-10-06
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


# ── bottom_mult 週期趨勢外插（v1.4）──────────────────────────────────
# 背景：bottom_mult 三輪 [0.131, 0.157, 0.225] 單調遞增（每輪底部漸淺、市場成熟）。
# 舊版用 median/p25（把三點當無序樣本）會把當前輪底部預測得「太深」——峰值側早有
# _apply_diminishing_returns 做週期遞減，熊底側卻無對稱的趨勢修正。此處補上：對
# cycle_index 做線性迴歸外插，得當前輪的 bottom_mult 點估與上下band。
# n=3 樣本脆弱，band 用固定相對寬度涵蓋模型不確定。
# 非對稱 band（2026-06）：deep(悲觀) 放寬至 -25%、shallow(樂觀) 維持 +15%。
# 理由：2026-02 ETF 放大波動（BlackRock 現貨 ETF 單日成交 $100億、機構參與）部分反證
# 「底部越來越淺」單調假設，自 2025-10 高點回撤已達 52.5%（史上第7大）→ 下尾需加防護，
# 深方向放寬以涵蓋「跌幅回到前輪 84% 線」的尾部情境；點估與淺方向不動。
_BOTTOM_TREND_BAND_DEEP    = 0.25   # 點估 -25% 為最深(悲觀)目標
_BOTTOM_TREND_BAND_SHALLOW = 0.15   # 點估 +15% 為最淺(樂觀)目標
_BOTTOM_MULT_FLOOR         = 0.08   # 外插下限保護（避免極端外插）
_BOTTOM_MULT_CAP           = 0.50   # 外插上限保護


def _fit_bottom_mult_trend():
    """對已完成週期的 bottom_mult ~ cycle_idx 做最小平方線性迴歸。回傳 (slope, intercept)。"""
    xs = list(range(len(_bottom_mults)))   # 0,1,2,...（已完成週期序）
    ys = _bottom_mults
    n  = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    intercept = my - slope * mx
    return slope, intercept


_BM_SLOPE, _BM_INTERCEPT = _fit_bottom_mult_trend()


def extrapolate_bottom_mult(cycle_idx: int):
    """
    回傳當前週期（cycle_idx，對齊 HALVING_DATES index）外插的 bottom_mult：
      (point, deep, shallow) — 點估 / 最深(悲觀) / 最淺(樂觀)
    cycle_idx 對應「第 N 個已完成週期序」用 idx 本身（第4輪 idx=3 即外插序 3）。
    """
    raw = _BM_INTERCEPT + _BM_SLOPE * cycle_idx
    point = min(max(raw, _BOTTOM_MULT_FLOOR), _BOTTOM_MULT_CAP)
    deep    = max(point * (1 - _BOTTOM_TREND_BAND_DEEP),    _BOTTOM_MULT_FLOOR)
    shallow = min(point * (1 + _BOTTOM_TREND_BAND_SHALLOW), _BOTTOM_MULT_CAP)
    return point, deep, shallow


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
        "cycle_ath_date":    _utcnow_naive(),
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


# ══════════════════════════════════════════════════════════════════════════
# 四季論 v2：時間×市場二維狀態機（B1，2026-07-06 新增；design：
# Github\Cow\season_v2_design.md）。SEASON_ENGINE="v1"（預設，config.py）時
# 以下函式不被 forecast_price 呼叫、零行為變更；"v2" 才啟用，切換前須完成
# design §4.2 回放對照三準則，屬受保護設定變更。
# ══════════════════════════════════════════════════════════════════════════

_M_HYSTERESIS_DAYS = 3   # 連續 N 個交易日一致才切換市場軸狀態
_M_WINDOW_DAYS = 15      # 防抖掃描回溯天數（含 hysteresis 窗）


def _raw_market_axis(dd: float, up: bool) -> str:
    """單日原始市場軸判定（無防抖）：bull（多頭確認）/ mid（未確認）/ bear（空頭確認）。"""
    if dd > -0.10 and up:
        return "bull"
    if dd < -0.20 and not up:
        return "bear"
    return "mid"


def _derive_market_axis(df: Optional[pd.DataFrame], current_halving: datetime,
                        as_of: Optional[datetime] = None):
    """
    市場軸 M（v2，3 日防抖）。純函數：從 df 倒推最後 _M_WINDOW_DAYS 個交易日的
    原始 M 狀態序列，逐日推進「生效狀態」——連續 _M_HYSTERESIS_DAYS 日一致且與
    現行生效狀態不同才切換，否則沿用（dd 在門檻邊界抖動時不反覆翻轉）。
    無需外部持久化：生效狀態由本次算的整段窗推出。

    回傳 (state, raw_trace)：state ∈ {"bull","mid","bear"}；raw_trace 為窗內
    逐日原始判定（供測試/除錯，非防抖後的結果）。df 不足時回 ("mid", [])。
    """
    if df is None or df.empty or "close" not in df.columns:
        return "mid", []
    df_naive = _strip_tz(df)
    if as_of is not None:
        cutoff = _tz_safe_timestamp(as_of)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_localize(None)
        df_naive = df_naive[df_naive.index <= cutoff]
    if df_naive.empty:
        return "mid", []
    n = len(df_naive.index)
    lookback = min(_M_WINDOW_DAYS, n)
    raw_trace = []
    for i in range(n - lookback, n):
        day_df = df_naive.iloc[: i + 1]
        price_i = float(day_df["close"].iloc[-1])
        ms_i = analyze_market_state(price_i, day_df, current_halving)
        raw_trace.append(_raw_market_axis(ms_i["drawdown_from_ath"], ms_i["is_above_sma200"]))
    effective = raw_trace[0]
    for i in range(1, len(raw_trace)):
        if i >= _M_HYSTERESIS_DAYS - 1:
            window = raw_trace[i - (_M_HYSTERESIS_DAYS - 1): i + 1]
            if len(set(window)) == 1 and window[0] != effective:
                effective = window[0]
    return effective, raw_trace


# 十二象限查表（design §2）：(時間軸 T, 市場軸 M) → {forecast_type, eff_season, conf_cap, note}。
# forecast_type ∈ {"bull_peak","bear_bottom","observe"}；observe 不輸出 target_low/mid/high。
# eff_season 供 rationale 顯示與 bull_peak/bear_bottom 計算沿用既有 v1 公式（設計刻意不換公式，
# 只換「哪個象限走哪條路」——C-2 修復格 (b) 即 autumn×bull 從 v1 的熊底誤判改為延長牛市）。
_SEASON_V2_TABLE: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("spring", "bull"): {"forecast_type": "bull_peak",   "eff_season": "spring", "conf_cap": 80, "note": None},
    ("spring", "mid"):  {"forecast_type": "bull_peak",   "eff_season": "spring", "conf_cap": 55, "note": None},
    ("spring", "bear"): {"forecast_type": "bear_bottom", "eff_season": "winter", "conf_cap": 65,
                        "note": "提前深熊（左尾週期，歷史無先例，偏離歷史模式）"},
    ("summer", "bull"): {"forecast_type": "bull_peak",   "eff_season": "summer", "conf_cap": 85, "note": None},
    ("summer", "mid"):  {"forecast_type": "bull_peak",   "eff_season": "summer", "conf_cap": 50, "note": "動能受阻"},
    ("summer", "bear"): {"forecast_type": "bear_bottom", "eff_season": "autumn", "conf_cap": 70, "note": "提前入秋"},
    ("autumn", "bull"): {"forecast_type": "bull_peak",   "eff_season": "summer_ext", "conf_cap": 45,
                        "note": "時間入秋、市場未確認——延長牛市，目標僅供參考"},
    ("autumn", "mid"):  {"forecast_type": "observe",     "eff_season": "autumn", "conf_cap": 35,
                        "note": "轉折觀察期，不出目標價"},
    ("autumn", "bear"): {"forecast_type": "bear_bottom", "eff_season": "autumn", "conf_cap": 80, "note": None},
    ("winter", "bull"): {"forecast_type": "observe",     "eff_season": "early_spring?", "conf_cap": 30,
                        "note": "時間已冬但創新高——疑新輪提前，勿套舊週期模板"},
    ("winter", "mid"):  {"forecast_type": "bear_bottom", "eff_season": "winter", "conf_cap": 60, "note": "打底觀察"},
    ("winter", "bear"): {"forecast_type": "bear_bottom", "eff_season": "winter", "conf_cap": 80, "note": None},
}
assert set(_SEASON_V2_TABLE.keys()) == {
    (t, m) for t in ("spring", "summer", "autumn", "winter") for m in ("bull", "mid", "bear")
}, "十二象限查表缺漏——每個 (T,M) 組合都必須有定義（design §2 無遺漏原則）"


def derive_effective_state(time_season: str, market_axis: str) -> Dict[str, Any]:
    """v2 核心：十二象限查表。回傳 {forecast_type, eff_season, conf_cap, note} 的複本
    （呼叫端可安全修改）。未定義象限 raise ValueError——這是設計錯誤不是資料問題，
    不應吞掉（_SEASON_V2_TABLE 模組層 assert 已保證跑到這裡時查表必中）。"""
    key = (time_season, market_axis)
    if key not in _SEASON_V2_TABLE:
        raise ValueError(f"四季論 v2 未定義象限：{key}")
    return dict(_SEASON_V2_TABLE[key])


def _resolve_ath_ref_v2(current_price: float, df: Optional[pd.DataFrame], current_halving: datetime,
                        current_cycle_idx: int, prev_ath: float, time_season: str,
                        market_state: Dict[str, Any]):
    """v2 的 ath_ref 選擇（design §3，廢除 v1 的 best_cycle_ath>current×1.05 全域魔數規則）。
    僅在 forecast_type=='bear_bottom' 時呼叫。除 T-spring 左尾情境外，一律用「當前週期
    ATH（實算與已知取 max）」——頂已印出/未確認皆錨當前週期，v1 的模糊 1.05 門檻只保留
    給 spring×bear 這個「當前週期尚無有意義高點」的情境（用同一道 1.05 檢查判斷「有意義」）。
    回傳 (ath_ref, ath_ref_label)。"""
    known_cycle = next((c for c in CYCLE_HISTORY if c["halving"] == current_halving), None)
    known_cycle_ath = known_cycle["ath_price"] if known_cycle and known_cycle["ath_price"] else None
    cycle_ath_ms = market_state.get("cycle_ath", 0)
    cycle_candidates = [v for v in (known_cycle_ath, cycle_ath_ms) if v]
    best_cycle_ath = max(cycle_candidates) if cycle_candidates else 0

    has_meaningful_ath = bool(best_cycle_ath and best_cycle_ath > current_price * 1.05)

    if time_season == "spring" and not has_meaningful_ath:
        return prev_ath, f"前一週期 ATH ${prev_ath:,.0f}（當前週期尚無有意義高點，左尾情境）"

    if not has_meaningful_ath:
        # 非 spring 情境下當前週期 ATH 仍不明確（極早期）——退化用現價本身避免除零/負數，
        # 這是邊界防呆非常態路徑（正常情況下能觸發 bear_bottom 象限時 ATH 必已印出）。
        best_cycle_ath = max(best_cycle_ath, current_price)

    if cycle_ath_ms and cycle_ath_ms >= (known_cycle_ath or 0):
        label = f"當前週期實算 ATH ${cycle_ath_ms:,.0f}"
    else:
        label = f"當前週期已知 ATH ${known_cycle_ath:,.0f}" if known_cycle_ath else f"當前週期 ATH ${best_cycle_ath:,.0f}"
    return best_cycle_ath, label


def get_current_season(as_of: Optional[datetime] = None):
    """計算「時間季節」（純減半週期時間位置，不含市場校正）。"""
    if as_of is None:
        as_of = _utcnow_naive()

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


def _current_cycle_known_peak_mult(cycle_idx: int) -> Optional[float]:
    """若該週期 ATH 已印出（CYCLE_HISTORY 有 peak_mult，含 is_complete=False 的進行中輪），
    回傳已知 peak_mult；否則 None。

    用途（v1.6 A1）：以「已發生的事實」校正牛市等比遞減外推。第4輪等比外推 peak_mult≈8.74x，
    但實際已知 ATH 僅 1.70x（ETF/機構化使漲幅塌縮），高估 5 倍。對 ATH 已印出的當前週期，
    改以已知值重錨；未來尚無 ATH 的週期回 None、維持等比外插不變。"""
    if cycle_idx < 0 or cycle_idx >= len(HALVING_DATES):
        return None
    halving = HALVING_DATES[cycle_idx]
    c = next((c for c in CYCLE_HISTORY if c["halving"] == halving), None)
    if c and c.get("peak_mult"):
        return float(c["peak_mult"])
    return None


def project_bear_bottom(current_price: float, df: Optional[pd.DataFrame] = None,
                        as_of: Optional[datetime] = None,
                        market_state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    熊市底部「原始投影」——不受當前季節影響，永遠回傳「若形成熊底會落在哪」。
    為 forecast_price（熊市分支）與 bottom_floors 的**單一真實來源**，杜絕兩邊漂移。

    market_state 可由呼叫端（如 forecast_price）傳入已算好的結果以複用，避免重複
    跑 analyze_market_state（含 rolling(200)）；為 None 時內部自算。

    回傳 dict:
      ath_ref / ath_ref_label
      bottom_mult_point / _deep / _shallow
      bottom_mid / bottom_low / bottom_high   （= ath_ref × mult，未對現價截斷的原始投影）
      current_cycle_idx / drawdown_from_ath / is_above_sma200 / sma200
    """
    if as_of is None:
        as_of = _utcnow_naive()
    season_info = get_current_season(as_of)
    if season_info is None:
        return None

    current_halving   = season_info["halving_date"]
    current_cycle_idx = HALVING_DATES.index(current_halving)

    prev_ath = None
    if df is not None and not df.empty and "close" in df.columns:
        df_naive   = _strip_tz(df)
        halving_ts = pd.Timestamp(current_halving)
        if current_cycle_idx > 0:
            prev_halving = HALVING_DATES[current_cycle_idx - 1]
            mask_prev = (df_naive.index >= pd.Timestamp(prev_halving)) & (df_naive.index < halving_ts)
            if mask_prev.any():
                prev_ath = float(df_naive.loc[mask_prev, "close"].max())

    known_cycle = next((c for c in CYCLE_HISTORY if c["halving"] == current_halving), None)
    known_cycle_ath = known_cycle["ath_price"] if known_cycle and known_cycle["ath_price"] else None
    if prev_ath is None:
        prev_ath = CYCLE_HISTORY[-2]["ath_price"] if len(CYCLE_HISTORY) >= 2 else 68789.0

    if market_state is None:
        market_state = analyze_market_state(current_price, df, current_halving)
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

    bm_point, bm_deep, bm_shallow = extrapolate_bottom_mult(current_cycle_idx)
    return {
        "ath_ref":             ath_ref,
        "ath_ref_label":       ath_ref_label,
        "bottom_mult_point":   bm_point,
        "bottom_mult_deep":    bm_deep,
        "bottom_mult_shallow": bm_shallow,
        "bottom_mid":          ath_ref * bm_point,
        "bottom_low":          ath_ref * bm_deep,      # 跌更深（悲觀）
        "bottom_high":         ath_ref * bm_shallow,   # 跌較淺（樂觀）
        "current_cycle_idx":   current_cycle_idx,
        "drawdown_from_ath":   market_state["drawdown_from_ath"],
        "is_above_sma200":     market_state["is_above_sma200"],
        "sma200":              market_state["sma200"],
    }


def forecast_price(current_price: float, df: Optional[pd.DataFrame] = None, as_of: Optional[datetime] = None,
                   season_engine: Optional[str] = None):
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

    [B1 / 2026-07-06] season_engine 參數（"v1"|"v2"，None 時讀 config.SEASON_ENGINE，
    預設 "v1"）：v2 走十二象限二維狀態機（derive_effective_state），新增
    forecast_type="observe"（target_low/mid/high 皆回 None——寧可不預測不出錯錨）。
    v1 路徑本次零改動；v2 沿用同一套 bull_peak/bear_bottom 計算公式，只換「哪個
    象限走哪條路」與 ath_ref 選擇規則（design：Github\\Cow\\season_v2_design.md）。
    """
    if as_of is None:
        as_of = _utcnow_naive()

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

    # v1.4：當前週期 ATH 解析已內聚於 project_bear_bottom（熊市分支單一來源）；
    # forecast_price 僅保留 prev_ath（牛市分支 halving_price + 回傳 prev_ath 用）。
    if prev_ath is None:
        prev_ath = CYCLE_HISTORY[-2]["ath_price"] if len(CYCLE_HISTORY) >= 2 else 68789.0

    market_state = analyze_market_state(current_price, df, current_halving)

    if season_engine is None:
        try:
            from config import SEASON_ENGINE as _cfg_engine
        except Exception:
            _cfg_engine = "v1"
        season_engine = _cfg_engine

    v2_state = None
    if season_engine == "v2":
        # ═══ v2：十二象限二維狀態機（B1，2026-07-06）═══
        market_axis, _ = _derive_market_axis(df, current_halving, as_of)
        v2_state = derive_effective_state(season_info["season"], market_axis)
        forecast_type_gate = v2_state["forecast_type"]
        real_season = v2_state["eff_season"]
        _v2_label_map = {
            "summer_ext":    ("夏季（延長）— 時間入秋、市場未確認", "☀️"),
            "early_spring?": ("春季？— 時間仍冬但市場已創新高",     "🌱"),
        }
        if real_season in _v2_label_map:
            real_season_zh, real_emoji = _v2_label_map[real_season]
        else:
            _lm = {"spring": ("春季 — 復甦期", "🌱"), "summer": ("夏季 — 牛市高峰", "☀️"),
                  "autumn": ("秋季 — 泡沫破裂", "🍂"), "winter": ("冬季 — 熊市底部", "❄️")}
            real_season_zh, real_emoji = _lm.get(real_season, ("未知", "❓"))
        is_corrected = (real_season != season_info["season"]) or (market_axis != "mid")
        correction_reason = (
            f"【v2 十二象限】時間={season_info['season']}／市場={market_axis}"
            + (f" → {v2_state['note']}" if v2_state["note"] else "")
        )
    else:
        # ═══ v1：既有散裝校正邏輯（本次零改動）═══
        real_season, real_season_zh, real_emoji, correction_reason, is_corrected = _derive_real_season(
            time_season     = season_info["season"],
            drawdown        = market_state["drawdown_from_ath"],
            is_above_sma200 = market_state["is_above_sma200"],
            month_in_cycle  = season_info["month_in_cycle"],
        )
        forecast_type_gate = "bull_peak" if real_season in ("spring", "summer") else "bear_bottom"

    effective_season = {
        "season":    real_season,
        "season_zh": real_season_zh,
        "emoji":     real_emoji,
    }

    adj_peak_med = _apply_diminishing_returns(STATS["peak_mult_median"], current_cycle_idx)
    adj_peak_p25 = _apply_diminishing_returns(STATS["peak_mult_p25"],    current_cycle_idx)
    adj_peak_p75 = _apply_diminishing_returns(STATS["peak_mult_p75"],    current_cycle_idx)

    # v1.6 A1：當前週期 ATH 已印出時，以已知 peak_mult 重錨等比遞減外推（保留 p25/p75 band 比例），
    # 杜絕第4輪外推 8.74x vs 實際 1.70x 的 5 倍高估。用事實校正、不新增可調參數；
    # 若牛市再創新高，下方「current_price > ath_target_med」分支仍會以現價續推上方空間。
    known_pm = _current_cycle_known_peak_mult(current_cycle_idx)
    if known_pm and adj_peak_med > 0:
        _rescale = known_pm / adj_peak_med
        adj_peak_med *= _rescale
        adj_peak_p25 *= _rescale
        adj_peak_p75 *= _rescale

    days_since = season_info["days_since"]
    ath_ref = None   # 僅 bear_bottom 分支設值；observe/bull_peak 維持 None

    if forecast_type_gate == "observe":
        # ═══ v2 專屬：觀察期，寧可不預測不出錯錨（design §2 note c/d）═══
        forecast_type = "observe"
        target_median = target_low = target_high = None
        estimated_date = None
        confidence = v2_state["conf_cap"]
        rationale = (
            f"【有效狀態】{real_emoji} {real_season_zh}\n"
            f"時間位置：第 {current_cycle_idx+1} 次減半後第 {season_info['month_in_cycle']} 個月\n"
            f"{v2_state['note']}\n"
            f"本狀態不輸出目標價（v2 observe 型：寧可不預測，不出錯錨的數字）。"
        )
        bear_label_low = bear_label_mid = bear_label_high = None

    elif forecast_type_gate == "bull_peak":
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
        if v2_state is not None:
            confidence = min(confidence, v2_state["conf_cap"])

        bear_label_low  = "保守目標（漲幅較小）"
        bear_label_mid  = "中位數目標"
        bear_label_high = "樂觀目標（漲幅較大）"

    else:
        # ═══ 熊市預測 ═══
        forecast_type = "bear_bottom"

        if v2_state is not None:
            # v2：ath_ref 選擇規則獨立於 v1（design §3，廢除 1.05 全域魔數）
            ath_ref, ath_ref_label = _resolve_ath_ref_v2(
                current_price, df, current_halving, current_cycle_idx, prev_ath,
                season_info["season"], market_state,
            )
            bm_point, bm_deep, bm_shallow = extrapolate_bottom_mult(current_cycle_idx)
            bottom_med = ath_ref * bm_point
            bottom_p25 = ath_ref * bm_deep
            bottom_p75 = ath_ref * bm_shallow
        else:
            # v1.4：熊底投影改由 project_bear_bottom 單一真實來源產出（ath_ref 解析 +
            #       bottom_mult 週期趨勢外插），forecast_price 與 bottom_floors 共用、杜絕漂移。
            bb            = project_bear_bottom(current_price, df, as_of, market_state=market_state)
            ath_ref       = bb["ath_ref"]
            ath_ref_label = bb["ath_ref_label"]
            bm_point      = bb["bottom_mult_point"]
            bottom_med = bb["bottom_mid"]   # 趨勢點估
            bottom_p25 = bb["bottom_low"]   # 跌更深（悲觀）
            bottom_p75 = bb["bottom_high"]  # 跌較淺（樂觀）

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
            f"底部倍數（週期趨勢外插）: {bm_point:.3f}（跌 {(1-bm_point)*100:.0f}%）"
            f"｜歷史三輪 13.1%→15.7%→22.5% 遞增（底部漸淺）\n"
            f"預計熊市底部區間: ${target_low:,.0f} ~ ${target_high:,.0f}"
        )

        confidence = min(int(80 - abs(days_since - STATS["bottom_days_median"]) / 5), 80)
        confidence = max(confidence, 35)
        if market_state["drawdown_from_ath"] < -0.25:
            confidence = min(confidence + 10, 75)
        if v2_state is not None:
            confidence = min(confidence, v2_state["conf_cap"])

        # ▸ v1.3: 熊市標籤改為方向明確的描述
        bear_label_low  = "最深目標（歷史最大跌幅）"
        bear_label_mid  = "中位數目標（歷史平均）"
        bear_label_high = "最淺目標（最輕微熊市）"

    return {
        "season_info":         season_info,
        "market_state":        market_state,
        "effective_season":    effective_season,
        "forecast_type":       forecast_type,
        "target_median":       round(target_median, 0) if target_median is not None else None,
        "target_low":          round(target_low,    0) if target_low    is not None else None,
        "target_high":         round(target_high,   0) if target_high   is not None else None,
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
        "ath_ref":             round(ath_ref, 0) if ath_ref is not None else None,
        "season_engine":       season_engine,
    }


def get_cycle_comparison_table(df: pd.DataFrame = None):
    """
    返回歷史各週期比較表 (pd.DataFrame)。
    [v1.3] 第4週期標「進行中」，顯示已知ATH，底部欄位顯示「尚未完成」。
    [v1.5] 進行中週期的 ATH/倍數/達ATH天數改用 df 實算（與寫死值取 max），
           避免市場創新高後仍顯示舊高點（如 $108,268）而過時。
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
            # 進行中：以 df 實算當前週期 ATH，與寫死值取 max（市場可能已創新高）
            ath_price = c["ath_price"]
            ath_date  = c["ath_date"]
            if df is not None and not df.empty and "close" in df.columns:
                d = _strip_tz(df)
                cyc = d.loc[d.index >= pd.Timestamp(c["halving"]), "close"]
                if not cyc.empty and float(cyc.max()) > ath_price:
                    ath_price = float(cyc.max())
                    ath_date  = cyc.idxmax().to_pydatetime()
            peak_mult = ath_price / c["halving_price"] if c["halving_price"] else 0.0
            peak_days = (ath_date - c["halving"]).days
            rows.append({
                "週期":        f"第 {i+1} 次減半",
                "狀態":        "🔄 進行中",
                "減半日":      c["halving"].strftime("%Y-%m-%d"),
                "減半時價格":  f"${c['halving_price']:,.0f}",
                "牛市 ATH":    f"${ath_price:,.0f} ✓",
                "ATH 倍數":    f"{peak_mult:.2f}x",
                "達 ATH 天數": f"{peak_days} 天",
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
        start   = _utcnow_naive() + timedelta(days=1),
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