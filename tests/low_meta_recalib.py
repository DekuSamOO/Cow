"""
tests/low_meta_recalib.py
抄底總分分級門檻重校（2026-08-25）— 階梯重訂後保持「各級警戒的稀有度」不變。

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/low_meta_recalib.py

為什麼要重校：
  RSI／SOPR／F&G 三個子項改成「絕對 ∪ PiT 分位」後觸發率大幅上升
  （RSI 4.2%→46%、SOPR 2.8%→31%、F&G 32%→50%），總分分布整體上移。
  若不動 relative_low_meta 的門檻，「🟢 強力抄底訊號」會從罕見變常見 ——
  **使用者對「幾分算警戒」的既有直覺會被靜默破壞**。

作法（不引入新參數）：
  取舊制在全期的各門檻**百分位**，把新制門檻設在同一個百分位上。
  例：舊制 75 分若落在第 98.3 百分位，新制門檻就取新分布的第 98.3 百分位。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import tests.radar_subitem_audit as A
from core.relative_low import compute_relative_low_score
from service.etf_flow import _summarize

OLD_CUTS = [(75, "🟢 強力抄底訊號"), (60, "🟢 明確低估"),
            (45, "🟡 偏冷觀察"), (25, "⚪ 中性")]


def replay():
    """逐日重放完整抄底總分：old=不餵歷史（＝舊階梯行為）、new=餵歷史（重訂後）。"""
    btc, fund, mvrv, sopr, etf, fng = A.load_all()
    idx = btc.index[btc.index >= A.START]
    sopr_keys, fng_keys = sorted(sopr), sorted(fng)
    old_s, new_s, dates = [], [], []
    for d in idx:
        key = d.strftime("%Y-%m-%d")
        i = btc.index.get_loc(d)
        row, sub_df = btc.iloc[i], btc.iloc[:i + 1]
        f8h = float(fund.loc[d]) / 1095 if d in fund.index else None
        etf_pit = {k: v for k, v in etf.items() if k <= key}
        etf_sum = _summarize(etf_pit) if etf_pit else None
        common = dict(funding_8h=f8h, oi_stats=None, etf_summary=etf_sum,
                      sopr=sopr.get(key), fng=fng.get(key), mvrv_z=mvrv.get(key))
        s_old, _ = compute_relative_low_score(row, sub_df, **common)
        s_new, _ = compute_relative_low_score(
            row, sub_df, **common,
            sopr_hist=[float(sopr[k]) for k in sopr_keys if k <= key][-400:],
            fng_hist=[float(fng[k]) for k in fng_keys if k <= key][-400:])
        old_s.append(s_old)
        new_s.append(s_new)
        dates.append(d)
    return pd.Series(old_s, index=dates), pd.Series(new_s, index=dates)


def main():
    print("逐日重放抄底總分（舊階梯 vs 重訂後）…")
    old, new = replay()
    n = len(old)
    print("樣本 %d 日：%s ~ %s" % (n, old.index[0].date(), old.index[-1].date()))
    print("\n總分分布　　 平均　中位　 P75　 P90　 P95　 P99　 最大")
    for name, s in [("舊制", old), ("重訂後", new)]:
        print("  %-8s %6.1f %5.0f %5.0f %5.0f %5.0f %5.0f %5.0f"
              % (name, s.mean(), s.median(), s.quantile(.75), s.quantile(.90),
                 s.quantile(.95), s.quantile(.99), s.max()))

    print("\n分級門檻重校（保持稀有度）")
    print("  等級                 舊門檻  舊稀有度   新門檻  新稀有度")
    new_cuts = []
    for cut, name in OLD_CUTS:
        rarity = float((old >= cut).mean())
        newcut = int(round(float(np.quantile(new, 1 - rarity)))) if rarity > 0 else int(new.max()) + 1
        new_rarity = float((new >= newcut).mean())
        new_cuts.append((name, cut, newcut))
        print("  %-18s %5d   %6.2f%%   %5d   %6.2f%%"
              % (name, cut, rarity * 100, newcut, new_rarity * 100))

    print("\n→ 建議寫入 relative_low_meta：")
    for name, old_cut, new_cut in new_cuts:
        print("    score >= %-3d  %s   （舊 %d）" % (new_cut, name, old_cut))
    print("\n⚠️ 這只保持『觸發頻率』不變，不保證『同一天的等級』不變 ——"
          "\n   重訂的目的就是讓等級更貼近真實底部，等級變動是預期內的。")

    # ── 用「真實底部當天的得分」校最高級，而不是挑一個好看的數字 ─────────────
    btc, _, _, _, _, _ = A.load_all()
    btc = btc.loc[new.index]
    close, low = btc["close"].values, btc["low"].values
    bots = A.swings(low, close, len(btc), False)
    print("\n" + "=" * 78)
    print("真實底部當天的抄底總分（n=%d，order=10 且其後 60 日反彈 >=18%%）" % len(bots))
    print("=" * 78)
    ov, nv = old.values, new.values
    b_old = np.array([ov[i] for i in bots])
    b_new = np.array([nv[i] for i in bots])
    for name, arr in [("舊制", b_old), ("重訂後", b_new)]:
        print("  %-8s 中位 %.0f｜P75 %.0f｜P90 %.0f｜最高 %.0f｜>=60 的比例 %.0f%%"
              % (name, np.median(arr), np.quantile(arr, .75), np.quantile(arr, .90),
                 arr.max(), (arr >= 60).mean() * 100))
    print("\n  → 最高級門檻應該落在「真實底部的高分段」而不是永遠碰不到的位置：")
    for q in (0.75, 0.90, 0.95):
        print("     真實底部的 P%-3d ＝ %d 分（重訂後）" % (q * 100, round(np.quantile(b_new, q))))
    print("  ⚠️ 這是用事件當天的分數回頭校門檻，屬 in-sample；門檻只用於**分級文案**、"
          "\n     不進任何交易條件，故可接受。若日後要拿分級當進場條件，需另做 holdout 驗證。")


if __name__ == "__main__":
    main()
