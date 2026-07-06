#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/season_v2_replay.py — 四季論 v1 vs v2 回放對照（B1 採用前必做，season_v2_design.md §4.2）

逐日跑 forecast_price(season_engine="v1") 與 "v2"，比較 (effective_season,
forecast_type) 是否一致，輸出差異日清單＋design §4.2 三條驗收準則的檢查結果。

⚠ 資料限制（誠實揭露，非隱藏）：本地 db/cache/BTC_HISTORY.csv 始於 2017-08-17，
早於此的 2013-2015（第 1 輪熊底）無法回放；第 2 輪（2018-19）、第 3 輪（2022）
熊底皆在涵蓋範圍內，可驗。

本腳本只產出對照數據與準則檢查結果，**不自動判定是否切換 SEASON_ENGINE**
——三條準則是否算「通過」需人工核對後裁定（受保護設定變更，config.py 註解已註明）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.season_forecast import forecast_price

HERE = os.path.dirname(os.path.abspath(__file__))
WARMUP = 250   # sma200 等指標暖機所需最少天數

KNOWN_BOTTOMS = [
    ("2018-11-01", "2019-02-28", "2018-19 熊底"),
    ("2022-11-01", "2023-01-31", "2022 FTX 熊底"),
]


def load_df():
    path = os.path.join(os.path.dirname(HERE), "db", "cache", "BTC_HISTORY.csv")
    return pd.read_csv(path, parse_dates=["date"]).set_index("date")


def main():
    t0 = time.time()
    df = load_df()
    n = len(df)
    rows = []
    for i in range(WARMUP, n):
        as_of = df.index[i]
        sub = df.iloc[: i + 1]
        price = float(sub["close"].iloc[-1])
        fc1 = forecast_price(price, sub, as_of=as_of, season_engine="v1")
        fc2 = forecast_price(price, sub, as_of=as_of, season_engine="v2")
        if fc1 is None or fc2 is None:
            continue
        v1_season = fc1["effective_season"]["season"]
        v2_season = fc2["effective_season"]["season"]
        rows.append({
            "date": as_of, "price": price,
            "v1_season": v1_season, "v1_type": fc1["forecast_type"],
            "v2_season": v2_season, "v2_type": fc2["forecast_type"],
            "diff": (fc1["forecast_type"] != fc2["forecast_type"]) or (v1_season != v2_season),
        })
    res = pd.DataFrame(rows).set_index("date")
    diffs = res[res["diff"]]
    elapsed = time.time() - t0

    lines = []
    lines.append(f"回放範圍：{res.index[0].date()} ~ {res.index[-1].date()}（{len(res)} 天，耗時 {elapsed:.0f}s）")
    lines.append("⚠ 資料限制：本地 BTC_HISTORY.csv 始於 2017-08-17，2013-2015 第 1 輪熊底無法回放。")
    lines.append(f"差異天數：{len(diffs)} / {len(res)}（{len(diffs) / len(res) * 100:.1f}%）")
    lines.append("")

    lines.append("== 準則 1：差異應集中在 T-秋/冬×M-牛 與 T-秋×M-中（design §2 目標格）==")
    lines.append("（依 v1_type→v2_type→v2_season 分組計數；佔比高者若落在 summer_ext/observe 即符合設計意圖）")
    if not diffs.empty:
        grp = diffs.groupby(["v1_type", "v2_type", "v2_season"]).size().sort_values(ascending=False)
        lines.append(grp.to_string())
    else:
        lines.append("（無差異天）")
    lines.append("")

    lines.append("== 準則 2：歷史真熊底期間 v1/v2 forecast_type 一致（不得倒退）==")
    for start, end, label in KNOWN_BOTTOMS:
        seg = res.loc[start:end] if (start in res.index.astype(str).values or True) else pd.DataFrame()
        seg = res[(res.index >= start) & (res.index <= end)]
        if seg.empty:
            lines.append(f"  {label}：資料範圍外或無資料，略過")
            continue
        mismatch = int((seg["v1_type"] != seg["v2_type"]).sum())
        verdict = "OK 一致" if mismatch == 0 else "NEEDS_REVIEW 有分歧"
        lines.append(f"  {label}（{start}~{end}，{len(seg)} 天）：type 不一致 {mismatch} 天 → {verdict}")
    lines.append("")

    v1_switches = int((res["v1_season"] != res["v1_season"].shift()).sum())
    v2_switches = int((res["v2_season"] != res["v2_season"].shift()).sum())
    lines.append("== 準則 3：M 軸防抖後，狀態切換次數不得比 v1 更抖 ==")
    lines.append(f"  v1 effective_season 切換次數：{v1_switches}")
    verdict3 = "OK 未更抖" if v2_switches <= v1_switches else "NEEDS_REVIEW 更抖"
    lines.append(f"  v2 eff_season 切換次數：{v2_switches} → {verdict3}")
    lines.append("")

    lines.append(f"差異日清單（前 300 筆，共 {len(diffs)} 筆）：")
    lines.append(diffs.head(300).to_string())

    out_path = os.path.join(HERE, "season_v2_replay_result.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"done：{len(res)} 天，{len(diffs)} 差異天，耗時 {elapsed:.0f}s，寫入 {out_path}")


if __name__ == "__main__":
    main()
