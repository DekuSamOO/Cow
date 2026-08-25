"""
core/pit_ladder.py · PiT 滾動分位階梯（單一真實來源）

2026-08-25 建立。動機見 tests/radar_subitem_audit.py：雷達多數子項的原始指標
AUC 0.63~0.78 是好訊號，但上線的**絕對門檻**校準自不同的市場環境，
把訊號壓到 0.50~0.56（抄底 RSI 落差達 +0.224）。

設計紀律（吸取同日資費混合版撤回的教訓：手挑一檔 cutoff 就足以撐起整個結論）：
  **只有一組級距表，所有子項共用，不逐項調參。**
  任何「這個子項特別調一下」的念頭都要先問：憑什麼？有沒有 holdout 支持？

純函數、零網路、零 pandas 依賴 → 易測、可被 core 各處與回測共用。
"""
import math
from typing import Optional, Sequence

# 共用級距：(分位門檻, 佔該子項配分的比例)
# 高值為極端（超買／過熱／貪婪）用 HIGH；低值為極端（超賣／低估／恐慌）用 LOW。
LADDER_HIGH = ((95.0, 1.00), (90.0, 0.75), (80.0, 0.50), (65.0, 0.25), (50.0, 0.10))
LADDER_LOW = ((5.0, 1.00), (10.0, 0.75), (20.0, 0.50), (35.0, 0.25), (50.0, 0.10))

DEFAULT_WINDOW = 365     # 週期型指標需一年以上才涵蓋一輪季節性
DEFAULT_MIN_OBS = 180


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def pit_percentile(hist: Optional[Sequence], current=None,
                   min_obs: int = DEFAULT_MIN_OBS,
                   window: int = DEFAULT_WINDOW) -> Optional[float]:
    """
    current 在「過去 window 日」中的分位（0–100，midrank 處理同值）。

    hist：**已截到評分當日為止**的序列（不含未來 → PiT）。呼叫端負責切「不含未來」，
          **本函式負責切「只看最近 window 日」**（2026-08-25 獨立檢核抓過一次：
          呼叫端餵 900 日、常數寫 180，生產與校準口徑不一致而無人察覺）。
    樣本不足 min_obs 回 None → 呼叫端應退回絕對階梯，不可硬算。
    """
    if hist is None:
        return None
    vals = [float(v) for v in hist if not _nan(v)]
    if window and window > 0:
        vals = vals[-window:]
    if len(vals) < min_obs:
        return None
    x = float(vals[-1]) if _nan(current) else float(current)
    below = sum(1 for v in vals if v < x)
    equal = sum(1 for v in vals if v == x)
    return (below + 0.5 * equal) / len(vals) * 100


def percentile_score(pct: Optional[float], max_points: int,
                     high_is_extreme: bool = True) -> int:
    """分位 → 分數（共用級距，四捨五入到整數）。pct=None 回 0。"""
    if pct is None or _nan(pct):
        return 0
    ladder = LADDER_HIGH if high_is_extreme else LADDER_LOW
    for thr, frac in ladder:
        hit = pct >= thr if high_is_extreme else pct <= thr
        if hit:
            return int(round(max_points * frac))
    return 0


def percentile_label(pct: Optional[float], window: int = DEFAULT_WINDOW) -> str:
    """面板用的分位說明（純事實陳述，不下方向判斷）。"""
    if pct is None or _nan(pct):
        return ""
    return f"P{pct:.0f}/{window}日"


def series_tail(seq, upto_index: int, window: int = DEFAULT_WINDOW) -> list:
    """把序列切成「到 upto_index 為止的最近 window 筆」——回測時的 PiT 切片小工具。"""
    lo = max(0, upto_index - window + 1)
    return [None if _nan(v) else float(v) for v in seq[lo:upto_index + 1]]
