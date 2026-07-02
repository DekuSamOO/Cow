"""
scripts/us_universal_backtest.py  ·  美股逃頂/抄底三維校準（swing-only AUC）

美股框架（core/relative_high_us / relative_low_us）目前**整支從未在美股資料上回測**，
權重 technical 50 / vol_price 30 / structure 20 全是專家經驗值。本腳本用與台股
tw_universal_backtest 相同的 swing-only 方法，在一籃美股上跑 out-of-sample AUC，
決定三維權重是否要按實測重配。

美股無免費籌碼源 → 三維全部只吃 OHLCV：
  technical  = relative_high_tw._score_technical_high / relative_low_tw._score_technical_low
               （需 RSI_14 + MACD → core.indicators.calculate_technical_indicators）
  vol_price  = relative_universal.score_volume_price_top/bottom
  structure  = relative_universal.score_structure_top/bottom

⚠️ 資料源 = Yahoo v8 chart（service.ohlc_universal.fetch_ohlc）——**公司網路 Yahoo 被 429 擋，
   需在家/雲端網路跑**。每檔間 sleep 避免限流。
用法：python scripts/us_universal_backtest.py [--tickers AAPL,MSFT,...] [--rng 10y] [--sleep 1.5]
"""
import os
import sys
import time
import argparse

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from service.ohlc_universal import fetch_ohlc                       # noqa: E402
from core.indicators import calculate_technical_indicators          # noqa: E402
from core.relative_high_tw import _score_technical_high             # noqa: E402
from core.relative_low_tw import _score_technical_low               # noqa: E402
from core.relative_universal import (score_volume_price_top, score_volume_price_bottom,  # noqa: E402
                                     score_structure_top, score_structure_bottom)
from tw_dim_backtest import auc                                     # noqa: E402

_SPLIT = "2024-01-01"
_REV = 0.18
_W = 10
_FWD = 60
_STRUCT_WIN = 140

# 預設一籃流動性高、跨產業的美股（可用 --tickers 覆蓋）。校準用途，非投資建議。
_DEFAULT = ("AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,JPM,V,UNH,XOM,JNJ,WMT,PG,MA,HD,"
            "CVX,ABBV,KO,PEP,COST,MRK,BAC,CRM,NFLX,AMD,INTC,DIS,CSCO,ORCL,QCOM,TXN,"
            "BA,CAT,GE,NKE,PFE,T,VZ,C,GS,MS,GM,F,PYPL,SBUX,MCD,IBM,GILD")


def _prep(ticker: str, rng: str) -> pd.DataFrame:
    """抓一檔美股日線 + 指標，回傳含 close/high/low/volume/RSI_14/MACD 的 df（失敗回空）。"""
    df = fetch_ohlc(ticker, rng=rng)
    df = calculate_technical_indicators(df, backtest_mode=True)
    df["price"] = df["close"].astype(float)
    df["fwd_ret"] = df["price"].shift(-_FWD) / df["price"] - 1
    win = 2 * _W + 1
    rmin = df["price"].rolling(win, center=True, min_periods=_W + 1).min()
    rmax = df["price"].rolling(win, center=True, min_periods=_W + 1).max()
    df["is_swing_low"] = df["price"] <= rmin
    df["is_swing_high"] = df["price"] >= rmax
    df["ticker"] = ticker
    return df


def _scores_at(df: pd.DataFrame, rows: pd.DataFrame, side: str):
    """對 swing 點逐點算三維分數（吃近 _STRUCT_WIN 列的 trailing 窗）。side='low'/'high'。"""
    out = {"technical": {}, "vol_price": {}, "structure": {}}
    arr = df.reset_index(drop=True)
    pos_map = {ix: i for i, ix in enumerate(df.index)}
    for ix in rows.index:
        pos = pos_map[ix]
        win = arr.iloc[max(0, pos - _STRUCT_WIN):pos + 1]
        row = win.iloc[-1]
        if side == "low":
            out["technical"][ix] = _score_technical_low(row, win)["score"]
            out["vol_price"][ix] = score_volume_price_bottom(win)["score"]
            out["structure"][ix] = score_structure_bottom(win)["score"]
        else:
            out["technical"][ix] = _score_technical_high(row, win)["score"]
            out["vol_price"][ix] = score_volume_price_top(win)["score"]
            out["structure"][ix] = score_structure_top(win)["score"]
    return {k: pd.Series(v) for k, v in out.items()}


def _report(title, frame):
    print(f"══ {title} ══")
    print(f"  {'維度':<18}{'AUC':>7}{'真':>8}{'假':>8}  判讀")
    for name, col in (("technical 技術", "technical"), ("vol_price 量價", "vol_price"),
                      ("structure 結構", "structure")):
        a, n1, n0 = auc(frame[col], frame["real"], True)
        if a is None:
            print(f"  {name:<18}{'—':>7}{n1:>8}{n0:>8}  樣本不足"); continue
        v = "🟢 有訊號(≥.55)" if a >= 0.55 else ("🟡 弱(≥.52)" if a >= 0.52 else
                                                ("⚪ 近雜訊" if a >= 0.48 else "🔴 方向反(<.48)"))
        print(f"  {name:<18}{a:>7.3f}{n1:>8,}{n0:>8,}  {v}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=_DEFAULT)
    ap.add_argument("--rng", default="10y")
    ap.add_argument("--sleep", type=float, default=1.5, help="每檔間隔秒（避 Yahoo 429）")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    print(f"[1] 抓 {len(tickers)} 檔美股日線（rng={args.rng}，每檔 sleep {args.sleep}s）…")
    frames, fails = [], []
    for i, t in enumerate(tickers, 1):
        try:
            frames.append(_prep(t, args.rng))
            print(f"    [{i}/{len(tickers)}] {t} ✓")
        except Exception as e:
            fails.append(t); print(f"    [{i}/{len(tickers)}] {t} ✕ {e}")
        time.sleep(args.sleep)
    if not frames:
        print("全部抓取失敗（多半是公司網路 Yahoo 被擋）→ 換家用/雲端網路再跑。"); sys.exit(1)
    if fails:
        print(f"    失敗 {len(fails)} 檔：{','.join(fails)}")

    alldf = pd.concat(frames)
    test = alldf[alldf.index >= pd.Timestamp(_SPLIT)]
    lows = test[test["is_swing_low"] & test["fwd_ret"].notna()].copy()
    highs = test[test["is_swing_high"] & test["fwd_ret"].notna()].copy()
    print(f"[2] test swing low {len(lows):,} / high {len(highs):,}；逐點算三維分數…")

    # 逐檔算（trailing 窗須同檔），再合併
    def _fill(rows, side):
        parts = {"technical": [], "vol_price": [], "structure": []}
        for tk, sub_rows in rows.groupby("ticker"):
            dsub = alldf[alldf["ticker"] == tk]
            sc = _scores_at(dsub, sub_rows, side)
            for k in parts:
                parts[k].append(sc[k])
        for k in parts:
            rows[k] = pd.concat(parts[k]).reindex(rows.index) if parts[k] else np.nan
        return rows

    lows = _fill(lows, "low")
    highs = _fill(highs, "high")
    lows["real"] = lows["fwd_ret"] >= _REV
    highs["real"] = highs["fwd_ret"] <= -_REV
    print(f"    真底 {int(lows['real'].sum()):,}/{len(lows):,}｜"
          f"真頂 {int(highs['real'].sum()):,}/{len(highs):,}\n")

    print("###### 美股三維 swing-only AUC（out-of-sample ≥2024）######\n")
    print(f"（現行專家權重：technical 50 / vol_price 30 / structure 20）\n")
    _report("抄底 swing low：分辨真底 vs 假底", lows)
    _report("逃頂 swing high：分辨真頂 vs 假頂", highs)
    print("判讀：AUC≥0.55 該維有效、可加權；<0.48 方向反→移除。依實測 AUC 比例重配 50/30/20。")


if __name__ == "__main__":
    main()
