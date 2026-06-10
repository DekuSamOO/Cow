"""
core/radar_replay.py
三雷達歷史每日分數回放 — 純 pandas/numpy，無 Streamlit 依賴

目的：
  逃頂（relative_high）/ 抄底（relative_low）/ 趨勢（trend_direction）原本只算
  「當日」分數；本模組逐日重放歷史分數序列，供 dashboard 回測 Tab 視覺化驗證，
  並產出「分數跨越門檻 → 其後 N 日報酬分布」統計（未來重校
  config.ESCAPE_ALERT_THRESHOLD 的依據）。

可回放輸入（歷史可得、無前視）：
  - 技術/長週期/趨勢維度：完全由日線 df 派生（背離只看 tail(120)，指標欄位均為因果計算）。
  - 資金費率子項：日均 8h% 序列（2021+，service/onchain.fetch_aux_history）。
  - F&G 子項：alternative.me 全史（2018+，service/realtime.fetch_fng_history）。
不可回放（與線上灰燈一致給 0 分）：OI 分位、ETF、SOPR、BTC.D、macro。
→ 回放分數是「歷史當下可得資訊」的保守下界，口徑與 GH Actions 雲端評分一致。
"""
from typing import Optional

import numpy as np
import pandas as pd

from core.relative_high import compute_escape_top_score
from core.relative_low import compute_relative_low_score
from core.trend_direction import compute_trend_score

# 背離偵測 tail(120) + 緩衝；逐日只切這段，避免 O(n²) 整段複製
# （handler/components/backtest_radar 切片暖機亦引用此值，單一來源）
DIV_WINDOW = 140
# 各指標欄位（SMA200 等）暖機所需的最少天數
DEFAULT_WARMUP = 250


def _day_inputs(df, i, fund_daily, fng_map):
    """第 i 天的 (row, 視窗df, 當日funding, 當日fng)。"""
    row = df.iloc[i]
    sub = df.iloc[max(0, i - DIV_WINDOW):i + 1]
    d = df.index[i]
    f = None
    if fund_daily is not None and len(fund_daily):
        v = fund_daily.get(d.normalize() if hasattr(d, "normalize") else d)
        f = None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
    g = None
    if fng_map:
        g = fng_map.get(d.strftime("%Y-%m-%d"))
    return row, sub, f, g


def _score_series(df, scorer, name, fund_daily=None, fng_map=None, start=DEFAULT_WARMUP):
    """逐日回放共用迴圈：scorer(row, sub, funding, fng) → (score, meta)，取 score。"""
    out = {}
    for i in range(start, len(df)):
        row, sub, f, g = _day_inputs(df, i, fund_daily, fng_map)
        out[df.index[i]] = scorer(row, sub, f, g)[0]
    return pd.Series(out, dtype=float, name=name)


def escape_score_series(
    df: pd.DataFrame,
    fund_daily: Optional[pd.Series] = None,
    fng_map: Optional[dict] = None,
    start: int = DEFAULT_WARMUP,
) -> pd.Series:
    """逐日回放逃頂分數（0-100）。df 需已過 indicators/ahr999/bear_bottom 計算。"""
    return _score_series(
        df, lambda r, s, f, g: compute_escape_top_score(r, s, funding_8h=f, fng=g),
        "escape_score", fund_daily, fng_map, start)


def low_score_series(
    df: pd.DataFrame,
    fund_daily: Optional[pd.Series] = None,
    fng_map: Optional[dict] = None,
    start: int = DEFAULT_WARMUP,
) -> pd.Series:
    """逐日回放抄底分數（0-100）。df 需已過 indicators/ahr999/bear_bottom 計算。"""
    return _score_series(
        df, lambda r, s, f, g: compute_relative_low_score(r, s, funding_8h=f, fng=g),
        "low_score", fund_daily, fng_map, start)


def trend_score_series(df: pd.DataFrame, start: int = DEFAULT_WARMUP) -> pd.Series:
    """逐日回放趨勢淨方向分（-100~+100）。"""
    return _score_series(
        df, lambda r, s, f, g: compute_trend_score(r, s), "trend_score", start=start)


def threshold_forward_stats(
    scores: pd.Series,
    close: pd.Series,
    thresholds=(45, 60, 75),
    horizon: int = 60,
    mode: str = "top",
    cooldown: int = 30,
) -> pd.DataFrame:
    """
    「分數向上跨越門檻」事件的其後 horizon 日報酬分布。

    mode="top"：驗證逃頂 — 命中 = 其後 horizon 日內最大回撤 ≤ -18%
    mode="bottom"：驗證抄底 — 命中 = 其後 horizon 日內最大漲幅 ≥ +18%
    （±18%/60 日與權重擬合時的正樣本定義一致，見 tests/relative_*_backtest.py）

    cooldown：兩次事件至少間隔天數，避免門檻附近抖動重複計數。
    回傳 DataFrame：門檻 / 事件數 / 命中率 / 中位最大跌幅 / 中位最大漲幅 / 中位期末報酬。
    """
    close = close.reindex(scores.index).astype(float)
    vals = scores.values
    rows = []
    for thr in thresholds:
        events = []
        last_i = -10**9
        for i in range(1, len(vals)):
            if vals[i - 1] < thr <= vals[i] and (i - last_i) >= cooldown:
                events.append(i)
                last_i = i
        min_rets, max_rets, end_rets = [], [], []
        for i in events:
            fut = close.iloc[i + 1:i + 1 + horizon]
            if fut.empty or np.isnan(close.iloc[i]):
                continue
            base = close.iloc[i]
            min_rets.append(fut.min() / base - 1)
            max_rets.append(fut.max() / base - 1)
            end_rets.append(fut.iloc[-1] / base - 1)
        n = len(min_rets)
        if n == 0:
            rows.append({"門檻": thr, "事件數": 0, "命中率": np.nan,
                         "中位最大跌幅": np.nan, "中位最大漲幅": np.nan, "中位期末報酬": np.nan})
            continue
        if mode == "top":
            hit = sum(1 for r in min_rets if r <= -0.18) / n
        else:
            hit = sum(1 for r in max_rets if r >= 0.18) / n
        rows.append({
            "門檻": thr, "事件數": n, "命中率": hit,
            "中位最大跌幅": float(np.median(min_rets)),
            "中位最大漲幅": float(np.median(max_rets)),
            "中位期末報酬": float(np.median(end_rets)),
        })
    return pd.DataFrame(rows)
