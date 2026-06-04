"""
core/miner_cost.py
礦工成本模型（純數學，無 IO 依賴）
─────────────────────────────────────────────────────────────────────
重建任一日期的礦工成本：
  電費盈虧價(USD/BTC) = hashrate_ths × eff_jth(date)/1000 × 24 × rate / btc_per_day(date)
  all-in 成本         = 電費盈虧價 × ALLIN_FACTOR（含礦機折舊 + 場地 + 運維）

關鍵：
  ① btc_per_day 依減半日切換（50→25→12.5→6.25→3.125 BTC/block × 144）
  ② eff_jth(date)：全網平均礦機效率（J/TH）隨時間下降，用 anchor 分段線性插值
     ⚠️ eff 是本模型最大不確定來源，anchor 為業界粗估，非精確值
  ③ rate：電價預設 0.055 USD/kWh（與 scripts/daily_line_notify 一致）

歷史回測用途：對 2015 / 2018 / 2022 熊底，比較熊底價 vs 電費盈虧價 / all-in 成本。
"""
from datetime import datetime
from typing import Optional, List, Tuple
import bisect

ELECTRICITY_RATE = 0.055   # USD/kWh（全網平均，與既有推播一致）
ALLIN_FACTOR     = 1.6     # all-in / 純電費（礦機折舊 + 場地 + 運維的綜合加成；業界估 1.5~2.0）

# 減半日 → 之後每日全網產出（BTC/day = block_reward × 144）
_HALVINGS: List[Tuple[datetime, float]] = [
    (datetime(2009, 1, 3),  7200.0),   # 50 BTC
    (datetime(2012, 11, 28), 3600.0),  # 25
    (datetime(2016, 7, 9),  1800.0),   # 12.5
    (datetime(2020, 5, 11),  900.0),   # 6.25
    (datetime(2024, 4, 19),  450.0),   # 3.125
    (datetime(2028, 4, 17),  225.0),   # 1.5625（預估）
]

# 全網平均礦機效率 anchor（date, J/TH）——業界粗估，效率逐年下降
# 來源綜合：早期 GPU/FPGA→ASIC 演進、S9(16nm)→S17/S19→S19XP/S21
_EFF_ANCHORS: List[Tuple[datetime, float]] = [
    (datetime(2013, 1, 1), 2000.0),
    (datetime(2014, 6, 1),  800.0),
    (datetime(2016, 1, 1),  250.0),
    (datetime(2017, 6, 1),  130.0),
    (datetime(2018, 12, 1),  95.0),
    (datetime(2020, 6, 1),   60.0),
    (datetime(2022, 6, 1),   40.0),
    (datetime(2024, 4, 1),   28.0),
    (datetime(2026, 1, 1),   24.0),
]


def btc_per_day(date: datetime) -> float:
    """回傳該日期所屬減半 era 的每日全網 BTC 產出。"""
    val = _HALVINGS[0][1]
    for h_date, h_val in _HALVINGS:
        if date >= h_date:
            val = h_val
        else:
            break
    return val


def eff_jth(date: datetime) -> float:
    """全網平均礦機效率（J/TH），anchor 間線性插值，邊界外取端點值。"""
    dates = [a[0] for a in _EFF_ANCHORS]
    vals  = [a[1] for a in _EFF_ANCHORS]
    if date <= dates[0]:
        return vals[0]
    if date >= dates[-1]:
        return vals[-1]
    i = bisect.bisect_right(dates, date)
    d0, d1 = dates[i - 1], dates[i]
    v0, v1 = vals[i - 1], vals[i]
    frac = (date - d0).total_seconds() / (d1 - d0).total_seconds()
    return v0 + (v1 - v0) * frac


def electricity_breakeven(
    hashrate_ths: float,
    date: datetime,
    rate: float = ELECTRICITY_RATE,
    eff_override: Optional[float] = None,
) -> float:
    """純電費盈虧價（USD/BTC）。eff_override 可固定效率（與現行即時值對齊用）。"""
    eff = eff_override if eff_override is not None else eff_jth(date)
    cost_per_day = hashrate_ths * eff / 1000 * 24 * rate
    return cost_per_day / btc_per_day(date)


def all_in_cost(
    hashrate_ths: float,
    date: datetime,
    rate: float = ELECTRICITY_RATE,
    eff_override: Optional[float] = None,
    allin_factor: float = ALLIN_FACTOR,
) -> float:
    """all-in 生產成本（USD/BTC）＝ 電費盈虧價 × allin_factor。"""
    return electricity_breakeven(hashrate_ths, date, rate, eff_override) * allin_factor


def reconstruct_series(hashrate_by_date: dict, rate: float = ELECTRICITY_RATE):
    """
    從 {date(datetime|str): hashrate_ths} 重建礦工成本歷史。
    回傳 list[dict]：{date, hashrate_ths, eff_jth, btc_per_day, elec_cost, allin_cost}
    """
    out = []
    for d, h in sorted(hashrate_by_date.items(), key=lambda kv: kv[0]):
        dt = d if isinstance(d, datetime) else datetime.fromisoformat(str(d)[:10])
        if not h or h <= 0:
            continue
        elec = electricity_breakeven(h, dt, rate)
        out.append({
            "date":         dt,
            "hashrate_ths": h,
            "eff_jth":      eff_jth(dt),
            "btc_per_day":  btc_per_day(dt),
            "elec_cost":    elec,
            "allin_cost":   elec * ALLIN_FACTOR,
        })
    return out
