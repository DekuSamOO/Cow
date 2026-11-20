"""tw_universal_backtest 的批次量價分數必須與生產函式逐列一致（2026-11-20 立）。

立規原因＝余力 B 抓蟲 B5 No.14：批次版 `_vp_scores` 把成交量 groupby 當收盤價用，
檔頭宣稱「門檻鏡像 relative_universal」從沒被驗過；2026-07-02 的「量價逃頂 0.566 轉正式、
抄底 0.500 移除」都建立在這個錯的輸入上。這裡用生產端 `score_volume_price_top/bottom`
逐列重算對拍——換一套獨立實作驗，不用批次版自己驗自己。
"""
import os
import sys
import importlib.util

import numpy as np
import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.relative_universal import score_volume_price_top, score_volume_price_bottom  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "tw_universal_backtest", os.path.join(_REPO, "scripts", "tw_universal_backtest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _panel(n_stocks=3, days=60, seed=11):
    rng = np.random.default_rng(seed)
    frames = []
    for k in range(n_stocks):
        close = 50 * (k + 1) * np.exp(np.cumsum(rng.normal(0, 0.03, days)))
        # 量級刻意設成台股「股」的尺度（千萬級），舊 bug 下 close/量 ≈ 0
        vol = rng.lognormal(mean=16, sigma=0.6, size=days)
        frames.append(pd.DataFrame({"Stock_ID": f"S{k}", "close": close, "Volume": vol,
                                    "Date": pd.date_range("2024-01-01", periods=days)}))
    return pd.concat(frames, ignore_index=True)


def test_batch_vp_scores_match_production_row_by_row():
    tub = _load()
    df = _panel()
    g_vol = df.groupby("Stock_ID", sort=False)["Volume"]
    g_close = df.groupby("Stock_ID", sort=False)["close"]
    top, bot = tub._vp_scores(g_vol, g_close, df["close"])
    checked = 0
    seen_top, seen_bot = set(), set()
    for sid, sub in df.groupby("Stock_ID", sort=False):
        prod_df = sub.rename(columns={"Volume": "volume"}).reset_index()
        for j in range(19, len(prod_df)):            # j=19 是生產端第一個有效列（len>=20）
            win = prod_df.iloc[:j + 1]
            i = int(win["index"].iloc[-1])
            pt = score_volume_price_top(win)["score"]
            pb = score_volume_price_bottom(win)["score"]
            assert top[i] == pt, f"{sid} 第 {j} 列逃頂：批次 {top[i]} vs 生產 {pt}"
            assert bot[i] == pb, f"{sid} 第 {j} 列抄底：批次 {bot[i]} vs 生產 {pb}"
            checked += 1
            seen_top.add(pt)
            seen_bot.add(pb)
    assert checked >= 100
    # 前提：兩側 8／15 分兩檔都出現過，否則只驗到 0 分（審查建議，換 seed 也不會悄悄失效）
    assert {8, 15} <= seen_top and {8, 15} <= seen_bot


# 邊界手算（finance-calc-reviewer 2026-11-20 提供，等號剛好落在門檻上）
_UP = [100] * 10 + [110] * 5 + [105] * 5          # ret_now=105/110-1<0、ret_prior=+10%
_FLAT_UP = [100] * 15 + [101] * 5                  # ret_now=+1%
_EQ = [100] * 10 + [110] * 5 + [121] * 5           # ret_now=ret_prior=+10%


@pytest.mark.parametrize("vols,closes,side,expected", [
    ([100] * 15 + [180] * 5, _UP, "top", 15),      # 量比剛好 1.5
    ([135] * 15 + [195] * 5, _UP, "top", 8),       # 量比剛好 1.3
    ([170] * 15 + [90] * 5, _FLAT_UP, "bottom", 15),   # 量比剛好 0.6、ret_now>0
    ([170] * 15 + [90] * 5, [100] * 20, "bottom", 8),  # ret_now=0 → 只到 8 分
    ([650] * 15 + [450] * 5, _FLAT_UP, "bottom", 8),   # 量比剛好 0.75
    ([100] * 15 + [180] * 5, _EQ, "top", 0),       # ret_now=ret_prior → 無背離
])
def test_vp_boundary_cases(vols, closes, side, expected):
    tub = _load()
    df = pd.DataFrame({"Stock_ID": "X", "close": [float(c) for c in closes],
                       "Volume": [float(v) for v in vols]})
    top, bot = tub._vp_scores(df.groupby("Stock_ID")["Volume"],
                              df.groupby("Stock_ID")["close"], df["close"])
    prod = (score_volume_price_top if side == "top" else score_volume_price_bottom)(
        df.rename(columns={"Volume": "volume"}))["score"]
    batch = (top if side == "top" else bot).iloc[-1]
    assert prod == expected, "手算預期與生產端不符"
    assert batch == expected
