# -*- coding: utf-8 -*-
"""
tests/dual_buylow_attrib.py — 雙幣 BUY_LOW 不行權歸因分解（交接信 No.5，2026-07-13）

問題：C-7/C-8/C-9 修正後全史雙幣回測 −90%，結構因＝2017-10 被行權轉 USDT 後
卡五年、結算樣本 95.8% 在 USDT 態。但「BUY_LOW 為何幾乎不行權」有兩個候選因子
未分解：(a) is_bearish gate（EMA_20<SMA_50 時整段禁開 BUY_LOW）
(b) strike 規則太深（min(BB_Lower,S1) − ATR×(1+risk)×dyn，1 天期）。

方法（歸因不是調參；結論不得用於加碼——Cow 陷阱 22 照舊）：
  臂0 baseline：原引擎原資料。
  臂1 gate-off：原引擎，df 的 EMA_20 欄改 = SMA_50（is_bearish 恆 False；
       回測迴圈中 EMA_20 僅用於 is_bearish，故此中和=純移除 gate、零引擎複製）。
  分類器：逐日重演決策狀態機（僅分類不算權益），以「開單日期與引擎 trade_log
       完全一致」自我驗證後，統計 USDT 態決策日的去向（gate 擋掉/已開單/冷卻/週末），
       並對 gate 擋掉日按同 strike 公式算 would-be strike、對照其後 duration 日
       fixing 是否會行權（=gate 的反事實行權損失）。
  strike 因子上界：對實際開單的 BUY_LOW 日，若 strike 掛在規則允許的最鬆處
       （close×0.99 cap），有幾單會行權——任何 strike 規則在 cap 限制下的上界。

資料：db/cache/BTC_HISTORY.csv 全史，指標同 dashboard（calculate_technical_indicators
backtest_mode=True）。參數：dashboard 預設 call_risk=put_risk=0.5、cooldown=1。
輸出：tests/dual_buylow_attrib_result.txt
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.indicators import calculate_technical_indicators
from strategy.dual_invest import run_dual_investment_backtest

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(os.path.dirname(HERE), "db", "cache", "BTC_HISTORY.csv")
PUT_RISK = 0.5


def load_df():
    df = pd.read_csv(CSV, parse_dates=["date"], index_col="date")
    df = calculate_technical_indicators(df, backtest_mode=True)
    return df.dropna(subset=["EMA_20", "SMA_50", "ATR", "BB_Lower"])


def would_be_strike(row):
    """複製引擎 BUY_LOW strike 規則（strategy/dual_invest.py:387-393）。"""
    atr_pct = row["ATR"] / row["close"]
    dyn = 0.8 if atr_pct > 0.015 else (1.2 if atr_pct < 0.005 else 1.0)
    buf = row["ATR"] * (1 + PUT_RISK) * dyn
    if row.get("ADX", 0) > 25:
        buf *= 1.5
    base = min(row["BB_Lower"], row.get("S1", row["BB_Lower"]))
    return min(base - buf, row["close"] * 0.99)


def classify_days(daily, gate_on=True):
    """逐日重演決策狀態機（不算權益），回傳分類記錄與開單日清單（供對拍驗證）。"""
    idx = daily.index
    state, asset = "IDLE", "BTC"
    lock_end = cooldown_end = None
    strike = None
    ptype = ""
    recs, opens = [], []
    for i in range(len(idx) - 1):
        t = idx[i]
        row = daily.loc[t]
        if state == "LOCKED":
            if t < lock_end:
                continue
            fixing = row["close"]
            if ptype == "SELL_HIGH" and fixing >= strike:
                asset = "USDT"
            elif ptype == "BUY_LOW" and fixing <= strike:
                asset = "BTC"
            cooldown_end = t + pd.Timedelta(days=1)
            state = "IDLE"
        if state == "IDLE":
            if cooldown_end is not None and t < cooldown_end:
                if asset == "USDT":
                    recs.append((t, "cooldown", None, None))
                continue
            wd = t.weekday()
            if wd >= 5:
                if asset == "USDT":
                    recs.append((t, "weekend", None, None))
                continue
            duration = 3 if wd == 4 else 1
            settle_t = t + pd.Timedelta(days=duration)
            if settle_t > idx[-1]:
                continue
            bearish = (row["EMA_20"] < row["SMA_50"]) if gate_on else False
            if asset == "BTC":
                atr_pct = row["ATR"] / row["close"]
                dyn = 0.8 if atr_pct > 0.015 else (1.2 if atr_pct < 0.005 else 1.0)
                buf = row["ATR"] * (1 + 0.5) * dyn
                if row.get("ADX", 0) > 25:
                    buf *= 1.5
                if row.get("J", 50) < 20:
                    buf *= 1.2
                base = max(row["BB_Upper"], row.get("R1", row["BB_Upper"]))
                strike = max(base + buf, row["close"] * 1.01)
                ptype = "SELL_HIGH"
            else:
                if bearish:
                    # gate 擋掉：反事實——若仍照 strike 規則開單，會不會行權？
                    k = would_be_strike(row)
                    pos = idx.searchsorted(settle_t)
                    fx = daily.iloc[min(pos, len(idx) - 1)]["close"]
                    recs.append((t, "gated", k, fx <= k))
                    continue
                strike = would_be_strike(row)
                pos = idx.searchsorted(settle_t)
                fx = daily.iloc[min(pos, len(idx) - 1)]["close"]
                # cap 上界：strike 掛最鬆（close×0.99）會不會行權
                recs.append((t, "open_buylow", strike,
                             (fx <= strike, fx <= row["close"] * 0.99)))
                ptype = "BUY_LOW"
            state = "LOCKED"
            lock_end = settle_t
            opens.append((t, ptype))
    return recs, opens


def main():
    daily = load_df()
    L = [f"雙幣 BUY_LOW 不行權歸因（全史 {daily.index[0].date()}~{daily.index[-1].date()}，"
         f"put_risk=0.5，cooldown=1）", ""]

    # ── 兩臂真引擎重放 ──
    log0 = run_dual_investment_backtest(daily)
    df_off = daily.copy()
    df_off["EMA_20"] = df_off["SMA_50"]          # gate 中和（is_bearish 恆 False）
    log1 = run_dual_investment_backtest(df_off)

    def arm_stats(log, tag):
        st = log[log["Action"] == "Settlement"]
        bl = st[st["Note"].str.contains("抄底|賺U")]
        ex = st[st["Note"].str.contains("抄底")]
        eq = log.iloc[-1]["Equity_BTC"]
        usdt_days = 0
        prev_t, prev_a = None, "BTC"
        for _, r in st.iterrows():
            if prev_t is not None and prev_a == "USDT":
                usdt_days += (r["Time"] - prev_t).days
            prev_t, prev_a = r["Time"], r["Asset"]
        L.append(f"[{tag}] 結算 {len(st)}（BUY_LOW {len(bl)}，其中行權 {len(ex)}）  "
                 f"USDT 態曆日 {usdt_days}  期末 Equity_BTC {eq:.4f}（{(eq-1)*100:+.1f}%）")
        return len(ex), usdt_days, eq

    arm_stats(log0, "臂0 baseline")
    arm_stats(log1, "臂1 gate-off")
    L.append("")

    # ── 分類器（自我驗證：開單日期對拍引擎）──
    recs0, opens0 = classify_days(daily, gate_on=True)
    eng_opens0 = [(r["Time"], r["Type"]) for _, r in log0[log0["Action"] == "Open"].iterrows()]
    match = opens0 == eng_opens0
    L.append(f"分類器自我驗證：開單序列與引擎 {'完全一致 PASS' if match else '不一致 FAIL——以下統計不可信'}"
             f"（{len(opens0)} vs {len(eng_opens0)} 單）")
    if match:
        gated = [r for r in recs0 if r[1] == "gated"]
        gated_ex = [r for r in gated if r[3]]
        ob = [r for r in recs0 if r[1] == "open_buylow"]
        ob_ex = [r for r in ob if r[3][0]]
        ob_cap = [r for r in ob if r[3][1]]
        cd = sum(1 for r in recs0 if r[1] == "cooldown")
        we = sum(1 for r in recs0 if r[1] == "weekend")
        L += [
            "",
            "== USDT 態決策日分解（baseline）==",
            f"  gate 擋掉：{len(gated)} 日（其中依同 strike 規則開單會行權：{len(gated_ex)} 次）",
            f"  實際開單 BUY_LOW：{len(ob)} 單（行權 {len(ob_ex)}；若 strike 掛最鬆 cap=99% 收盤，"
            f"上界行權 {len(ob_cap)}）",
            f"  冷卻 {cd} 日／週末 {we} 日",
        ]
    txt = "\n".join(L)
    with open(os.path.join(HERE, "dual_buylow_attrib_result.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
