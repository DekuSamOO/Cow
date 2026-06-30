"""core/backtest_robustness 單元測試（DSR + Monte Carlo）。"""
import numpy as np
import pytest

from core.backtest_robustness import (
    _norm_cdf, _norm_ppf,
    probabilistic_sharpe_ratio,
    expected_max_sharpe_pp,
    deflated_sharpe_ratio,
    monte_carlo_trades,
)


def test_norm_helpers():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(_norm_ppf(0.975) - 1.959963985) < 1e-4
    assert abs(_norm_cdf(_norm_ppf(0.83)) - 0.83) < 1e-6


def test_psr_basic():
    rng = np.random.default_rng(0)
    good = rng.normal(0.002, 0.01, 1000)     # 正期望、低波動 → 高 PSR
    bad = rng.normal(0.0, 0.01, 1000)        # 零期望 → PSR≈0.5
    assert probabilistic_sharpe_ratio(good, 0.0) > 0.95
    assert 0.3 < probabilistic_sharpe_ratio(bad, 0.0) < 0.7


def test_psr_degenerate():
    assert probabilistic_sharpe_ratio([0.0, 0.0, 0.0, 0.0]) is None   # 零波動
    assert probabilistic_sharpe_ratio([0.01]) is None                # 樣本不足


def test_expected_max_sharpe_grows_with_trials():
    v = 0.01
    sr1 = expected_max_sharpe_pp(10, v)
    sr2 = expected_max_sharpe_pp(1000, v)
    assert sr2 > sr1 > 0
    assert expected_max_sharpe_pp(1, v) == 0.0


def test_dsr_penalises_more_trials():
    rng = np.random.default_rng(1)
    rets = rng.normal(0.0015, 0.01, 800)
    d1 = deflated_sharpe_ratio(rets, n_trials=1)
    d50 = deflated_sharpe_ratio(rets, n_trials=50)
    # 嘗試越多，SR* 門檻越高、dsr 越低（不可能更高）
    assert d50["sr_star_annual"] >= d1["sr_star_annual"]
    assert d50["dsr"] <= d1["dsr"]
    assert d1["sr_annual"] == d50["sr_annual"]            # 觀測 Sharpe 不變


def test_monte_carlo_profitable():
    # 全為小幅獲利交易 → 高獲利機率
    trades = [3.0, -1.5, 4.0, 2.0, -1.0, 5.0, 1.5, -0.5] * 5
    out = monte_carlo_trades(trades, n_sims=500, seed=42)
    assert out["n_trades"] == 40
    assert out["prob_profit"] > 0.8
    # 分位單調
    r = out["roi_pct"]
    assert r["p5"] <= r["p25"] <= r["p50"] <= r["p75"] <= r["p95"]
    assert out["mdd_pct"]["p50"] >= 0.0


def test_monte_carlo_skip_and_empty():
    out = monte_carlo_trades([2.0, -1.0, 3.0], n_sims=100, skip_frac=0.34, seed=1)
    assert out["n_sims"] == 100
    empty = monte_carlo_trades([])
    assert empty["n_trades"] == 0 and empty["prob_profit"] is None
