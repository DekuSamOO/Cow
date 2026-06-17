"""
tests/alert_threshold_calib.py
逃頂/抄底警報門檻校準 — 用 core/radar_replay 歷史回放驗證/重校 config.ESCAPE_ALERT_THRESHOLD。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/alert_threshold_calib.py

方法：
  1. 逐日回放逃頂(0-100)/抄底(0-100)分數序列（radar_replay，前視防護）。
  2. threshold_forward_stats：分數「向上跨越門檻」事件 → 其後 60 日報酬分布、命中率
     （逃頂命中=回撤≤-18%；抄底命中=反彈≥+18%）。
  3. 對門檻 30~90 掃描，看「命中率(精度) × 事件頻率(不洗版)」找平衡，並對照無條件基準率。

⚠️ 重大限制（須誠實標示於結論）：
  回放分數是「歷史當下可得資訊」的保守下界——OI/ETF/SOPR/BTC.D/macro 在回放中一律給 0。
  逃頂可達天花板僅 ~55（資費20+技術25+F&G10），抄底 ~65（週期25+技術20+負費率10+F&G10）。
  故「門檻 60」對逃頂在回放中幾乎不可達 → 回放無法直接校準逃頂 60；只能對「回放可達區間」
  給有效性下界，真實 live（含 OI/ETF/SOPR）分數更高、門檻另需 live 對照。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings; warnings.filterwarnings("ignore")
import logging; logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
import requests, urllib3
urllib3.disable_warnings()

from service.market_data import fetch_market_data
from service.onchain import fetch_aux_history
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators
from core.radar_replay import escape_score_series, low_score_series, threshold_forward_stats

HORIZON = 60
MOVE = 0.18


def _load():
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)

    _, _, fund = fetch_aux_history()
    fund_daily = pd.Series(dtype=float)
    if fund is not None and not fund.empty and "fundingRate" in fund.columns:
        f = fund.copy()
        if f.index.tz is not None:
            f.index = f.index.tz_localize(None)
        fund_daily = f["fundingRate"].resample("D").mean()

    fng_map = {}
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=0&format=json",
                         timeout=20, verify=False)
        for it in r.json().get("data", []):
            d = pd.to_datetime(int(it["timestamp"]), unit="s").strftime("%Y-%m-%d")
            fng_map[d] = float(it["value"])
    except Exception as e:
        print("F&G 歷史抓取失敗：", e)
    return btc, fund_daily, fng_map


def _baseline(close, mode):
    """無條件基準率：任一日其後 60 日內出現 ≥18% 反向波動的比例（命中率的對照底）。"""
    c = close.values.astype(float)
    n = len(c)
    hits = 0; tot = 0
    for i in range(n - 1):
        fut = c[i + 1:i + 1 + HORIZON]
        if not len(fut):
            continue
        tot += 1
        move = (fut.min() / c[i] - 1) if mode == "top" else (fut.max() / c[i] - 1)
        if (move <= -MOVE) if mode == "top" else (move >= MOVE):
            hits += 1
    return hits / tot if tot else float("nan")


def _report(name, scores, close, mode, thresholds):
    print(f"\n=== {name} 門檻掃描（mode={mode}）===")
    print(f"回放分數範圍 {scores.min():.0f}~{scores.max():.0f}（n={scores.notna().sum()} 日）"
          f"｜可達天花板受限於回放下界")
    base = _baseline(close, mode)
    print(f"無條件基準率（任一日其後60日{'回撤≤-18%' if mode=='top' else '反彈≥+18%'}）= {base*100:.0f}%")
    stats = threshold_forward_stats(scores, close, thresholds=thresholds,
                                    horizon=HORIZON, mode=mode, cooldown=30)
    print(stats.to_string(index=False,
          formatters={"命中率": lambda v: f"{v*100:4.0f}%" if v == v else "—",
                      "中位最大跌幅": lambda v: f"{v*100:+.1f}%" if v == v else "—",
                      "中位最大漲幅": lambda v: f"{v*100:+.1f}%" if v == v else "—",
                      "中位期末報酬": lambda v: f"{v*100:+.1f}%" if v == v else "—"}))
    # 提升 = 命中率 − 基準率（>0 表示門檻有篩選力）
    print("提升(命中率−基準率)：", end=" ")
    for _, row in stats.iterrows():
        if row["事件數"] and row["命中率"] == row["命中率"]:
            print(f"≥{int(row['門檻'])}:{(row['命中率']-base)*100:+.0f}pp", end="  ")
    print()
    return stats, base


def main():
    print("載入資料 …")
    btc, fund_daily, fng_map = _load()
    close = btc["close"]
    escape = escape_score_series(btc, fund_daily, fng_map)
    low = low_score_series(btc, fund_daily, fng_map)

    esc_thr = [30, 35, 40, 45, 50, 55, 60]
    low_thr = [40, 45, 50, 55, 60, 65, 70]
    esc_stats, esc_base = _report("逃頂（ESCAPE）", escape, close, "top", esc_thr)
    low_stats, low_base = _report("抄底（LOW）", low, close, "bottom", low_thr)

    # ── 自動判讀（隨資料變動）─────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("自動判讀：")
    THR = 60   # config.ESCAPE_ALERT_THRESHOLD
    esc_reach = escape.max() >= THR
    low_reach = low.max() >= THR

    def _useful(stats, base):
        """回放可達區間是否有任一門檻命中率顯著高於基準率(+10pp 且事件數≥5)。"""
        ok = stats[(stats["事件數"] >= 5) & (stats["命中率"] >= base + 0.10)]
        return not ok.empty

    if not esc_reach:
        print(f"  逃頂：回放最高分 {escape.max():.0f} < 門檻 {THR} → **回放無法觸發/校準逃頂 {THR}**。"
              "（OI/ETF/SOPR=0 的保守下界所致；真實 live 分數更高。）")
    elif not _useful(esc_stats, esc_base):
        print(f"  逃頂：回放可達門檻在 {esc_base*100:.0f}% 基準率上無顯著篩選力 → 門檻校準暫不可靠。")
    else:
        print("  逃頂：回放可達區間中有門檻顯著優於基準率，可作 live 門檻參考（仍偏保守下界）。")

    if not _useful(low_stats, low_base):
        print(f"  抄底：回放分數對『其後60日反彈≥18%』無篩選力（命中率不高於 {low_base*100:.0f}% 基準率）→ "
              "抄底分高≠即將反彈（估值便宜可長期維持，符合『勿純憑估值接刀』）。")
    else:
        print("  抄底：回放可達區間有門檻優於基準率，可作 live 門檻參考。")

    print(f"\n  → 結論：現行 config.ESCAPE_ALERT_THRESHOLD={THR} 為保守專家設計，"
          "回放（OI/ETF/SOPR 缺）無法統計校準之；維持 60，待 Phase 3 OI/ETF/SOPR 歷史補齊後重跑本腳本校準。")


if __name__ == "__main__":
    main()
