"""
tests/momentum_backtest.py
Time-Series Momentum（Moskowitz-Ooi-Pedersen 2012）對 BTC 的實證檢驗 — 離線、跨週期。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/momentum_backtest.py

資料：本地 15m DB resample 日線（service.local_db_reader.read_btc_daily，2017–2026，
      跨 2018/2022 熊 + 2021 牛 + 2024 減半），無網路。

方法（無前視）：
  對每個 lookback L ∈ {90,180,365} 日：
  (1) 預測力：past = P(t)/P(t-L)-1（t 當下已知）；fwd = P(t+H)/P(t)-1（H=30 日）。
      測 sign(past) 命中 sign(fwd) 的命中率 + Mann-Whitney AUC（past 為分數、fwd>0 為正樣本）；
      時序前半 train、後半 test（不洗牌，避免前視）。
  (2) 策略：pos = sign(past_L)，日報酬 = pos.shift(1) × daily_ret（訊號昨日、今日進場）；
      翻倉扣單邊成本 0.1%。算年化報酬 / Sharpe(252) / MDD vs Buy&Hold。

AUC 以 Mann-Whitney U 實作（不引入 sklearn）。結論寫入 stdout 供人工判讀（是否值得進加權）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from service.local_db_reader import read_btc_daily

LOOKBACKS = (90, 180, 365)
HORIZON = 30          # 預測 forward 30 日
COST = 0.001          # 單邊 0.1%
ANN = 252


def auc(pos, neg):
    """Mann-Whitney U → AUC。pos/neg 為分數陣列。"""
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv), float); ranks[order] = np.arange(1, len(allv) + 1)
    # 平手取平均秩
    _, inv, cnt = np.unique(allv, return_inverse=True, return_counts=True)
    csum = np.cumsum(cnt); starts = csum - cnt
    avg = (starts + csum + 1) / 2.0
    ranks = avg[inv]
    r_pos = ranks[:len(pos)].sum()
    u = r_pos - len(pos) * (len(pos) + 1) / 2.0
    return u / (len(pos) * len(neg))


def _sharpe(daily_rets):
    r = pd.Series(daily_rets).dropna()
    if r.std(ddof=1) == 0 or len(r) < 2:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * np.sqrt(ANN))


def _mdd(equity):
    eq = np.asarray(equity, float)
    peak = np.maximum.accumulate(eq)
    return float(np.min(eq / peak - 1.0) * 100)


def _cagr(equity, n_days):
    yrs = n_days / 365.0
    return float((equity[-1] ** (1 / yrs) - 1) * 100) if yrs > 0 and equity[-1] > 0 else float("nan")


def main():
    df = read_btc_daily(start_date="2017-01-01")
    close = df["close"].dropna()
    print(f"資料：{close.index[0].date()} ~ {close.index[-1].date()}（{len(close)} 日）\n")

    daily_ret = close.pct_change()
    bh_eq = (1 + daily_ret.fillna(0)).cumprod().values
    print(f"{'Buy&Hold':<10} CAGR {_cagr(bh_eq, len(close)):>7.1f}%  Sharpe {_sharpe(daily_ret):>5.2f}  "
          f"MDD {_mdd(bh_eq):>7.1f}%\n")

    print(f"{'Lookback':<10}{'命中率':>8}{'AUC全期':>9}{'AUC測試半':>10}"
          f"{'策略CAGR':>10}{'Sharpe':>8}{'MDD':>9}{'換手/年':>9}")
    for L in LOOKBACKS:
        past = close / close.shift(L) - 1.0                 # t 當下已知
        # (1) 預測力
        fwd = close.shift(-HORIZON) / close - 1.0           # 未來（僅評估用，不進策略）
        mask = past.notna() & fwd.notna()
        p, f = past[mask], fwd[mask]
        hit = float(((np.sign(p) == np.sign(f)) & (f != 0)).mean() * 100)
        a_full = auc(p[f > 0], p[f <= 0])
        half = len(p) // 2                                  # 時序後半 test
        pt, ft = p.iloc[half:], f.iloc[half:]
        a_test = auc(pt[ft > 0], pt[ft <= 0])
        # (2) 策略（無前視：昨日訊號今日進場）
        pos = np.sign(past)
        strat = pos.shift(1) * daily_ret
        turnover = pos.diff().abs().fillna(0)               # 翻倉 |Δpos|（0→1=1、+1→-1=2）
        strat = strat - turnover * COST
        strat = strat.dropna()
        eq = (1 + strat).cumprod().values
        turns_per_yr = float(turnover[turnover > 0].count() / (len(close) / 365.0))
        print(f"{L}d({L//30}M)".ljust(10)
              + f"{hit:>7.1f}%{a_full:>9.3f}{a_test:>10.3f}"
              + f"{_cagr(eq, len(strat)):>9.1f}%{_sharpe(strat):>8.2f}{_mdd(eq):>8.1f}%{turns_per_yr:>9.1f}")

    print("\n判讀：AUC>0.55 且 策略 Sharpe 明顯優於 B&H 才考慮進 trend_direction 加權；")
    print("      否則維持『參考訊號不計分』（core/momentum.momentum_ref_rows）。")


if __name__ == "__main__":
    main()
