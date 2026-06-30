"""
core/backtest_robustness.py
回測穩健性指標（純函數，無 Streamlit / scipy 依賴）
─────────────────────────────────────────────────────────────────────
解決「UI 拉滑桿找報酬最高參數組 = 人肉 grid search = 過擬合」這個最大缺口：

1. Deflated Sharpe Ratio（DSR，Bailey & López de Prado 2014）
   依「嘗試的參數組數 N、報酬偏態/峰度、樣本長度」去膨脹 Sharpe，
   回傳「真實 Sharpe > 由 N 次嘗試運氣可達的期望最大 Sharpe」之機率。
   DSR < 0.95 → 該 Sharpe 很可能是多次嘗試挑出來的運氣，不是真 edge。

2. Monte Carlo 穩健性（對逐筆交易報酬 bootstrap 重抽）
   輸出 ROI / MDD 的分位數分佈與獲利機率，量化「單一 equity curve 的運氣成分」。

常態分配 CDF / 逆函數以 math.erf + Acklam 近似自實作，避免引入 scipy 依賴。
"""
from __future__ import annotations
from typing import Sequence, Optional, Dict, Any
import math
import numpy as np

_GAMMA = 0.5772156649015329   # Euler–Mascheroni 常數


# ── 常態分配工具（不依賴 scipy）─────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """標準常態分位函數（Acklam 有理近似，|誤差| < 1.15e-9）。"""
    if not (0.0 < p < 1.0):
        if p <= 0.0:
            return -math.inf
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _clean(returns: Sequence[float]) -> np.ndarray:
    r = np.asarray(list(returns), dtype=float)
    return r[~np.isnan(r)]


# ── Probabilistic / Deflated Sharpe Ratio ──────────────────────────
def probabilistic_sharpe_ratio(returns: Sequence[float],
                               sr_benchmark_pp: float = 0.0) -> Optional[float]:
    """PSR：真實 (per-period) Sharpe > sr_benchmark_pp 的機率，含偏態/峰度修正。

    returns          逐期報酬序列（per-period，非年化）
    sr_benchmark_pp  比較基準 Sharpe（per-period）；0 = 「Sharpe>0 的信心」
    """
    r = _clean(returns)
    T = len(r)
    sd = r.std(ddof=1) if T > 1 else 0.0
    if T < 3 or sd == 0:
        return None
    sr = r.mean() / sd                                   # per-period Sharpe
    m = r.mean()
    sp = r.std(ddof=0)                                   # 母體標準差（標準化動差用）
    z = (r - m) / sp
    skew = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))                        # Pearson（非超額）
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr))
    return _norm_cdf((sr - sr_benchmark_pp) * math.sqrt(T - 1) / denom)


def expected_max_sharpe_pp(n_trials: int, var_sr_pp: float) -> float:
    """N 次獨立嘗試下「期望最大 (per-period) Sharpe」SR*（Bailey-López de Prado）。

    var_sr_pp：各嘗試 per-period Sharpe 的變異數。
    """
    if n_trials <= 1 or var_sr_pp <= 0:
        return 0.0
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr_pp) * ((1.0 - _GAMMA) * z1 + _GAMMA * z2)


def deflated_sharpe_ratio(returns: Sequence[float],
                          n_trials: int = 1,
                          var_sr_pp: Optional[float] = None,
                          annualization: int = 252) -> Dict[str, Any]:
    """Deflated Sharpe Ratio。

    returns        所選策略的逐期報酬（per-period）
    n_trials       為挑出此策略而嘗試過的參數組數（UI 滑桿掃過幾組就填幾）
    var_sr_pp      各嘗試 per-period Sharpe 的變異數；None 時以「該 Sharpe 估計量本身的
                   抽樣變異」作代理（嘗試彼此相似時的常用簡化，已於回傳註明）。
    annualization  年化因子（日線 252，與 swing/walkforward 對齊）；僅供顯示換算。

    回傳 dict：sr_annual / sr_star_annual / psr / dsr / n_trials / var_sr_pp_used
      - dsr：真實 Sharpe 高於「N 次嘗試運氣可達的期望最大 Sharpe」之機率。
        經驗門檻 dsr ≥ 0.95 才視為通過去膨脹考驗。
    """
    r = _clean(returns)
    T = len(r)
    sd = r.std(ddof=1) if T > 1 else 0.0
    if T < 3 or sd == 0:
        return {"sr_annual": 0.0, "sr_star_annual": 0.0, "psr": None,
                "dsr": None, "n_trials": n_trials, "var_sr_pp_used": None,
                "note": "樣本不足或零波動"}
    sr_pp = r.mean() / sd
    # var_sr 代理：Sharpe 估計量抽樣變異（含偏態/峰度修正），/(T-1)
    m, sp = r.mean(), r.std(ddof=0)
    z = (r - m) / sp
    skew = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))
    if var_sr_pp is None:
        var_sr_pp = max(1e-12, (1.0 - skew * sr_pp + (kurt - 1.0) / 4.0 * sr_pp * sr_pp) / (T - 1))
        var_note = "var_sr_pp 用 Sharpe 抽樣變異代理（未提供跨嘗試分佈）"
    else:
        var_note = "var_sr_pp 由呼叫端提供（跨嘗試實際分佈）"
    sr_star_pp = expected_max_sharpe_pp(n_trials, var_sr_pp)
    ann = math.sqrt(annualization)
    # PSR/DSR 共用同一偏態/峰度修正分母 → 由上方已算的 skew/kurt/sr_pp 直接組，
    # 不再重複呼叫 probabilistic_sharpe_ratio（早退保證 T≥3 且 sd>0，故不會回 None）
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr_pp + (kurt - 1.0) / 4.0 * sr_pp * sr_pp))
    root = math.sqrt(T - 1)
    psr = _norm_cdf(sr_pp * root / denom)                    # 基準 Sharpe=0
    dsr = _norm_cdf((sr_pp - sr_star_pp) * root / denom)     # 基準 Sharpe=SR*
    return {
        "sr_annual":       round(sr_pp * ann, 3),
        "sr_star_annual":  round(sr_star_pp * ann, 3),
        "psr":             round(psr, 4),
        "dsr":             round(dsr, 4),
        "n_trials":        int(n_trials),
        "var_sr_pp_used":  var_sr_pp,
        "note":            var_note,
    }


# ── Monte Carlo 穩健性 ──────────────────────────────────────────────
def _equity_path_stats(pnl_fracs: np.ndarray, initial: float):
    """依序複利逐筆報酬，回傳 (roi_pct, mdd_pct)。"""
    equity = initial
    peak = initial
    max_dd = 0.0
    for r in pnl_fracs:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    roi = (equity / initial - 1.0) * 100.0
    return roi, max_dd * 100.0


def monte_carlo_trades(trade_pnl_pcts: Sequence[float],
                       initial_capital: float = 10_000.0,
                       n_sims: int = 2000,
                       skip_frac: float = 0.0,
                       seed: Optional[int] = None) -> Dict[str, Any]:
    """對逐筆交易報酬 bootstrap 重抽，量化單一 equity curve 的運氣成分。

    trade_pnl_pcts  逐筆交易報酬（%），即 result['trades'] 的 pnl_pct
    n_sims          模擬次數（產業常用 1,000–5,000）
    skip_frac       每次模擬隨機跳過的交易比例（測訊號依賴；0=純 bootstrap）
    回傳 roi_pct / mdd_pct 的 p5/p25/p50/p75/p95 與 prob_profit。
    """
    pnl = _clean(trade_pnl_pcts) / 100.0   # → 小數
    n = len(pnl)
    if n == 0:
        return {"n_trades": 0, "n_sims": 0, "roi_pct": None,
                "mdd_pct": None, "prob_profit": None}
    rng = np.random.default_rng(seed)
    keep = max(1, int(round(n * (1.0 - skip_frac))))
    rois = np.empty(n_sims)
    mdds = np.empty(n_sims)
    for s in range(n_sims):
        sample = rng.choice(pnl, size=keep, replace=True)
        rois[s], mdds[s] = _equity_path_stats(sample, initial_capital)

    def _pcts(arr):
        q = np.percentile(arr, [5, 25, 50, 75, 95])
        return {"p5": round(float(q[0]), 2), "p25": round(float(q[1]), 2),
                "p50": round(float(q[2]), 2), "p75": round(float(q[3]), 2),
                "p95": round(float(q[4]), 2)}

    return {
        "n_trades":    int(n),
        "n_sims":      int(n_sims),
        "skip_frac":   skip_frac,
        "roi_pct":     _pcts(rois),
        "mdd_pct":     _pcts(mdds),
        "prob_profit": round(float(np.mean(rois > 0)), 4),
    }
