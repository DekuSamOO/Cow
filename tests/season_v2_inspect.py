#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/season_v2_inspect.py — 四季論 v2 推進第一步：人工抽查（2026-09-02）

`歷程\\20260706findings_四季論v2回放對照.md` 三之3 明訂，下次推進 v2 的起點是：
  (a) 人工抽查發現 A/B 與 2022 熊底 17 天分歧的具體情境
  (b) 決定是否要擴充市場軸分級
  (c) 重新設計後再跑一次回放腳本

本腳本做 (a)。它**不改任何參數、不動 SEASON_ENGINE**，只把三處分歧的當日市場情境
攤開來看：dd（距 cycle ATH）、是否站上 sma200、減半後月數、v1/v2 各自的判定。

一律呼叫生產函式（forecast_price／analyze_market_state／get_current_season），
不自己重算——這是為了避免「用產生修改的那套邏輯驗自己」（全域 CLAUDE.md §4）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.season_forecast import (forecast_price, get_current_season,
                                  analyze_market_state, _derive_market_axis,
                                  HALVING_DATES)


def _cur_halving(as_of):
    past = [h for h in HALVING_DATES if h <= as_of]
    return past[-1] if past else None

HERE = os.path.dirname(os.path.abspath(__file__))
WARMUP = 250


def load_df():
    path = os.path.join(os.path.dirname(HERE), "db", "cache", "BTC_HISTORY.csv")
    return pd.read_csv(path, parse_dates=["date"]).set_index("date")


def build():
    df = load_df()
    rows = []
    for i in range(WARMUP, len(df)):
        as_of = df.index[i]
        sub = df.iloc[: i + 1]
        price = float(sub["close"].iloc[-1])
        fc1 = forecast_price(price, sub, as_of=as_of, season_engine="v1")
        fc2 = forecast_price(price, sub, as_of=as_of, season_engine="v2")
        if fc1 is None or fc2 is None:
            continue
        hv = _cur_halving(as_of.to_pydatetime())
        ms = (analyze_market_state(price, sub, hv) or {}) if hv else {}
        se = get_current_season(as_of) or {}
        m_axis = _derive_market_axis(sub, hv, as_of=as_of)[0] if hv else "n/a"
        rows.append({
            "date": as_of,
            "price": price,
            "dd": ms.get("drawdown_from_ath"),
            "up200": ms.get("is_above_sma200"),
            "m": se.get("month_in_cycle"),
            "Maxis": m_axis,
            "v1_season": fc1["effective_season"]["season"],
            "v1_type": fc1["forecast_type"],
            "v2_season": fc2["effective_season"]["season"],
            "v2_type": fc2["forecast_type"],
        })
    res = pd.DataFrame(rows).set_index("date")
    res["diff"] = (res["v1_type"] != res["v2_type"]) | (res["v1_season"] != res["v2_season"])
    return res


def fmt(seg, n=None):
    if seg.empty:
        return "   （無）"
    s = seg if n is None else seg.head(n)
    out = []
    for d, r in s.iterrows():
        dd = "n/a" if pd.isna(r["dd"]) else ("%+.1f%%" % (r["dd"] * 100))
        out.append("   %s $%8.0f dd=%-8s up200=%-5s m=%-3s M=%-5s| v1 %-13s/%-12s| v2 %-13s/%-12s"
                   % (d.date(), r["price"], dd, r["up200"], r["m"], r["Maxis"],
                      r["v1_season"], r["v1_type"], r["v2_season"], r["v2_type"]))
    if n is not None and len(seg) > n:
        out.append("   …（共 %d 天，僅列前 %d）" % (len(seg), n))
    return "\n".join(out)


def main():
    t0 = time.time()
    res = build()
    diffs = res[res["diff"]]
    L = []
    L.append("四季論 v2 人工抽查（2026-09-02）")
    L.append("回放範圍：%s ~ %s（%d 天）  差異 %d 天（%.1f%%）  耗時 %.0fs"
             % (res.index[0].date(), res.index[-1].date(), len(res),
                len(diffs), len(diffs) / len(res) * 100, time.time() - t0))
    L.append("")

    L.append("=" * 100)
    L.append("【差異分組】v1_type → v2_type → v2_season")
    L.append("=" * 100)
    if not diffs.empty:
        L.append(diffs.groupby(["v1_type", "v2_type", "v2_season"]).size()
                 .sort_values(ascending=False).to_string())
    L.append("")

    # ── 發現 A：v1 winter（深熊 override）vs v2 autumn ──
    A = diffs[(diffs["v1_season"] == "winter") & (diffs["v2_season"] == "autumn")]
    L.append("=" * 100)
    L.append("【發現 A】v1=winter（深熊 override）↔ v2=autumn —— 共 %d 天" % len(A))
    L.append("=" * 100)
    if not A.empty:
        L.append("   dd 分布：min %.1f%%  中位 %.1f%%  max %.1f%%"
                 % (A["dd"].min() * 100, A["dd"].median() * 100, A["dd"].max() * 100))
        L.append("   期間：%s ~ %s" % (A.index[0].date(), A.index[-1].date()))
        L.append("   ── 最深的 10 天（v2 在這些日子仍只給 autumn）──")
        L.append(fmt(A.nsmallest(10, "dd")))
        L.append("   ── 最淺的 5 天（分級邊界）──")
        L.append(fmt(A.nlargest(5, "dd")))
    L.append("")

    # ── 發現 B：v1 bull_peak → v2 bear_bottom（方向與設計意圖相反）──
    B = diffs[(diffs["v1_type"] == "bull_peak") & (diffs["v2_type"] == "bear_bottom")]
    L.append("=" * 100)
    L.append("【發現 B】v1=bull_peak ↔ v2=bear_bottom（方向與 C-2 修復相反）—— 共 %d 天" % len(B))
    L.append("=" * 100)
    L.append(fmt(B))
    L.append("")

    # ── 2022 FTX 熊底 17 天分歧 ──
    L.append("=" * 100)
    L.append("【準則 2】歷史真熊底期間 type 分歧")
    L.append("=" * 100)
    for start, end, label in [("2018-11-01", "2019-02-28", "2018-19 熊底"),
                              ("2022-11-01", "2023-01-31", "2022 FTX 熊底")]:
        seg = res[(res.index >= start) & (res.index <= end)]
        if seg.empty:
            L.append("  %s：無資料" % label)
            continue
        mm = seg[seg["v1_type"] != seg["v2_type"]]
        L.append("  %s（%s~%s，%d 天）：type 不一致 %d 天" % (label, start, end, len(seg), len(mm)))
        if not mm.empty:
            L.append(fmt(mm))
    L.append("")

    # ── 準則 3 ──
    v1s = int((res["v1_season"] != res["v1_season"].shift()).sum())
    v2s = int((res["v2_season"] != res["v2_season"].shift()).sum())
    L.append("=" * 100)
    L.append("【準則 3】切換次數  v1=%d  v2=%d  → %s" % (v1s, v2s, "OK" if v2s <= v1s else "更抖"))
    L.append("=" * 100)

    # ── 本輪（2026）現況：v1/v2 今天各說什麼 ──
    L.append("")
    L.append("【本輪現況】最後 10 天")
    L.append(fmt(res.tail(10)))

    out = os.path.join(HERE, "season_v2_inspect_result.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print("\n寫入 %s" % out)


if __name__ == "__main__":
    main()
