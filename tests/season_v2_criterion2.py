#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/season_v2_criterion2.py — 準則 2 修訂案的實證（2026-09-02）

原準則 2（season_v2_design.md §4.2）：「三輪真熊底期間 v1/v2 forecast_type 一致，不得倒退」。

**它與 v2 的設計目標從第一天就互相矛盾**——同一份設計文件 §2 明訂
`autumn×mid → observe`「轉折觀察期，不出目標價」，而熊底確認窗必然橫跨
「還在跌」與「已反彈」兩段；要求 v2 在反彈段仍與 v1 一致，
等於要求它放棄自己的設計目的。這個矛盾在設計時就可偵測，**不需要看資料**。

修訂案把一條準則拆成兩條，以「真底日」為界：

  2a（下跌段，硬性）：熊底事件的**最低點當日及之前**，兩版 forecast_type 必須一致。
                      ——v2 不得在價格還在創新低時就閉嘴，那才是真的倒退。
  2b（反彈段，寬鬆）：最低點之後，允許 v2 由 bear_bottom 轉 observe，
                      但**不得轉成 bull_peak**。轉 observe＝承認不確定（可接受）；
                      轉 bull_peak＝在熊底喊牛市（另一個方向的錯，不可接受）。

本腳本只產出數據，不自動裁定。

> ⚠️ **誠實揭露（憲法第 23 條 啟發集利息）**：本修訂案是在**已經看過**
> 2026-09-02 回放結果（19 天分歧全在反彈段）之後寫的。因此
> 「2a/2b 通過」**不足以單獨支撐切換 SEASON_ENGINE**——支撐它的是上面那個
> 「設計時就存在的邏輯矛盾」，資料只是佐證。若日後有新一輪熊底（unseen），
> 應以本準則重驗一次才算取得完整驗證資格。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.season_forecast import forecast_price

HERE = os.path.dirname(os.path.abspath(__file__))
WARMUP = 250

# (窗起, 窗迄, 標籤)——沿用 season_v2_replay.py 的 KNOWN_BOTTOMS，不自訂新窗
BOTTOM_WINDOWS = [
    ("2018-11-01", "2019-02-28", "2018-19 熊底"),
    ("2022-11-01", "2023-01-31", "2022 FTX 熊底"),
]


def main():
    path = os.path.join(os.path.dirname(HERE), "db", "cache", "BTC_HISTORY.csv")
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")

    rows = []
    for i in range(WARMUP, len(df)):
        as_of = df.index[i]
        sub = df.iloc[: i + 1]
        price = float(sub["close"].iloc[-1])
        f1 = forecast_price(price, sub, as_of=as_of, season_engine="v1")
        f2 = forecast_price(price, sub, as_of=as_of, season_engine="v2")
        if f1 is None or f2 is None:
            continue
        rows.append({"date": as_of, "close": price,
                     "v1": f1["forecast_type"], "v2": f2["forecast_type"]})
    res = pd.DataFrame(rows).set_index("date")

    print("=" * 92)
    print("準則 2 修訂案實證（以窗內最低收盤日為界，切下跌段／反彈段）")
    print("=" * 92)

    verdicts = []
    for start, end, label in BOTTOM_WINDOWS:
        seg = res[(res.index >= start) & (res.index <= end)]
        if seg.empty:
            print("\n%s：資料範圍外" % label)
            continue
        # 真底＝窗內最低收盤（與 leverage_window.find_bear_low 同口徑：最低 close）
        low_date = seg["close"].idxmin()
        down = seg[seg.index <= low_date]
        up = seg[seg.index > low_date]

        d_mis = down[down["v1"] != down["v2"]]
        u_mis = up[up["v1"] != up["v2"]]
        # 2b 的紅線：反彈段 v2 轉 bull_peak
        u_bad = u_mis[u_mis["v2"] == "bull_peak"]

        ok_2a = len(d_mis) == 0
        ok_2b = len(u_bad) == 0
        verdicts.append((label, ok_2a, ok_2b))

        print("\n%s（%s ~ %s，%d 天）" % (label, start, end, len(seg)))
        print("  真底（窗內最低收盤）：%s  $%s"
              % (low_date.date(), format(seg["close"].min(), ",.0f")))
        print("  下跌段 %3d 天 → type 不一致 %2d 天   [2a %s]"
              % (len(down), len(d_mis), "PASS" if ok_2a else "FAIL"))
        print("  反彈段 %3d 天 → type 不一致 %2d 天（其中轉 bull_peak %d 天） [2b %s]"
              % (len(up), len(u_mis), len(u_bad), "PASS" if ok_2b else "FAIL"))
        if len(u_mis):
            kinds = u_mis.groupby(["v1", "v2"]).size()
            for (a, b), n in kinds.items():
                print("      反彈段分歧型態：v1=%-12s → v2=%-12s  %d 天" % (a, b, n))
        if len(d_mis):
            print("      ⚠ 下跌段分歧日：")
            for d, r in d_mis.iterrows():
                print("        %s  $%8.0f  v1=%-12s v2=%s" % (d.date(), r["close"], r["v1"], r["v2"]))

    print("\n" + "=" * 92)
    all_2a = all(v[1] for v in verdicts)
    all_2b = all(v[2] for v in verdicts)
    print("彙總：2a（下跌段須一致）%s ｜ 2b（反彈段不得轉 bull_peak）%s"
          % ("PASS" if all_2a else "FAIL", "PASS" if all_2b else "FAIL"))
    print("=" * 92)
    print("⚠ 本準則寫於已看過回放結果之後（啟發集）；通過不等於取得完整驗證資格，")
    print("  支撐切換的是「原準則與設計目標邏輯矛盾」這個與資料無關的論證。")


if __name__ == "__main__":
    main()
