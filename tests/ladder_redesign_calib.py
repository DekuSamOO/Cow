"""
tests/ladder_redesign_calib.py
階梯重訂校準（2026-08-25）— 用「一條共用的 PiT 分位級距」取代各自手訂的絕對門檻。

手動執行（非 pytest，吃 db/cache 本機快取）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/ladder_redesign_calib.py

動機（radar_subitem_audit 的頭條發現）：
  原始指標 AUC 0.63~0.78 是好訊號，但上線的絕對階梯把它壓到 0.50~0.56。
  問題不在選錯指標，在門檻是絕對值、且校準自不同的市場環境。

設計紀律（吸取 2026-08-25 資費混合版撤回的教訓：手挑一檔就足以撐起整個結論）：
  **只有一條級距表，套用到所有子項，不逐項調參。**
    高值為極端： P>=95 -> 100% | >=90 -> 75% | >=80 -> 50% | >=65 -> 25% | >=50 -> 10%
    低值為極端： P<= 5 -> 100% | <=10 -> 75% | <=20 -> 50% | <=35 -> 25% | <=50 -> 10%
  分數 = round(該子項配分 x 比例)，與現行絕對階梯**取大值**
  （保留「跌破 200 週均」「MVRV-Z>=7」這類跨時代的結構意義）。

收案門檻（每個子項獨立判定，不整批採用）：
  重訂版必須在 **train 與 holdout 都** 贏現行版，才採用該子項；只贏一邊＝不採用。

樣本與 AUC 沿用 funding_threshold_calib 既有方法論（H=60、ORDER=10、MOVE=0.18、seed=0），
並已修正「未來窗不足 60 日不標記」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import tests.radar_subitem_audit as A
from core.pit_ladder import pit_percentile, percentile_score

TRAIN_END = "2024-01-01"

# (分數欄, 原始指標欄, 顯示名, 配分, 是頂側?, 高值為極端?, 可測旗標欄)
CANDIDATES = [
    ("T_rsi",      "raw_rsi",      "逃頂 RSI 超買",  7, True,  True,  None),
    ("T_mvrv",     "raw_mvrv",     "逃頂 MVRV-Z",    6, True,  True,  "has_mvrv"),
    ("T_sopr",     "raw_sopr",     "逃頂 SOPR",      8, True,  True,  "has_sopr"),
    ("T_fng",      "raw_fng",      "逃頂 F&G",      10, True,  True,  "has_fng"),
    ("L_rsi",      "raw_rsi",      "抄底 RSI 超賣",  6, False, False, None),
    ("L_mayer",    "raw_mayer",    "抄底 Mayer",    10, False, False, None),
    ("L_sma200w",  "raw_sma200w",  "抄底 200週均",   9, False, False, None),
    ("L_powerlaw", "raw_powerlaw", "抄底 冪律",      6, False, False, None),
    ("L_mvrv",     "raw_mvrv",     "抄底 MVRV-Z",    6, False, False, "has_mvrv"),
    ("L_sopr",     "raw_sopr",     "抄底 SOPR",      4, False, False, "has_sopr"),
    ("L_fng",      "raw_fng",      "抄底 F&G",      10, False, False, "has_fng"),
]

# ── 改動前的絕對階梯「凍結快照」（2026-08-25 獨立檢核 🟠 No.3）────────────────
# 為什麼要凍結：本腳本原本從 radar_subitem_audit.build_scores() 取「現行」基準，
# 但那支呼叫的是產品函式 —— 改動落地後 `cur` 就等於 `hyb`，決策依據自己把自己洗掉，
# 「11 項採用 3 項」再也重現不出來。基準必須是**改動前那一刻**的階梯，且不隨產品碼變動。
# 下列階梯抄自 commit aaca4cd 的 core/relative_low.py 與 core/relative_high.py。
FROZEN_LADDERS = {
    "T_rsi":      (lambda v: 7 if v >= 80 else 5 if v >= 75 else 3 if v >= 70 else 0),
    "L_rsi":      (lambda v: 6 if v <= 20 else 4 if v <= 25 else 2 if v <= 30 else 0),
    "L_mayer":    (lambda v: 10 if v < 0.8 else 6 if v < 1.0 else 3 if v < 1.2 else 0),
    "L_sma200w":  (lambda v: 9 if v < 1.0 else 6 if v < 1.3 else 3 if v < 2.0 else 0),
    "L_powerlaw": (lambda v: 6 if v < 2.0 else 3 if v < 5.0 else 0),
}


def frozen_current(col, raw_series):
    """改動前的整數分數序列；沒有凍結快照的子項（SOPR/F&G/MVRV）沿用 audit 的欄位。"""
    fn = FROZEN_LADDERS.get(col)
    if fn is None:
        return None
    out = np.zeros(len(raw_series), dtype=float)
    for i, v in enumerate(raw_series):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        out[i] = fn(float(v))
    return out


WINDOW = 365          # 滾動視窗（日）：週期型指標需一年以上才涵蓋一輪季節性
MIN_OBS = 180


def rolling_scores(raw, max_pts, high_extreme):
    """逐日 PiT 分位 → 重訂階梯分數（只用當日與之前）。"""
    out = np.zeros(len(raw), dtype=float)
    vals = [None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
            for v in raw]
    for i in range(len(vals)):
        if vals[i] is None:
            continue
        lo = max(0, i - WINDOW + 1)
        p = pit_percentile(vals[lo:i + 1], vals[i], min_obs=MIN_OBS, window=WINDOW)
        if p is None:
            continue
        out[i] = percentile_score(p, max_pts, high_extreme)
    return out


def main():
    print("載入與逐日重放 …（沿用 radar_subitem_audit 的資料層）")
    btc, sc = A.build_scores()
    close = btc["close"].values
    high, low = btc["high"].values, btc["low"].values
    n = len(btc)
    tops = A.swings(high, close, n, True)
    bots = A.swings(low, close, n, False)
    rng = np.random.default_rng(0)
    rr = range(A.ORDER, n - A.ORDER)
    ntp = list(rng.choice([k for k in rr if all(abs(k - t) > 30 for t in tops)],
                          size=min(len(tops) * 3, n), replace=False))
    nbt = list(rng.choice([k for k in rr if all(abs(k - t) > 30 for t in bots)],
                          size=min(len(bots) * 3, n), replace=False))
    split = int(np.searchsorted(btc.index.values, np.datetime64(TRAIN_END)))
    print("樣本 %d 日｜頂 %d／底 %d｜train/holdout 切點 %s"
          % (n, len(tops), len(bots), TRAIN_END))
    print("級距：高極端 P95/90/80/65/50 → 100/75/50/25/10%%；低極端鏡像。視窗 %d 日、min_obs %d"
          % (WINDOW, MIN_OBS))

    print("\n" + "=" * 104)
    print("%-16s%-5s%-9s%-9s%-9s%-9s%-9s%-9s  %s"
          % ("子項", "配分", "現train", "新train", "現hold", "新hold", "現觸發", "新觸發", "判定"))
    print("=" * 104)
    adopt, reject, replace_rows = [], [], []
    for col, rawcol, name, w, is_top, high_ex, flagcol in CANDIDATES:
        mask = sc[flagcol].values if flagcol else np.ones(len(sc), bool)
        ev, nev = (tops, ntp) if is_top else (bots, nbt)
        raw_vals = pd.to_numeric(sc[rawcol], errors="coerce").values
        frozen = frozen_current(col, raw_vals)          # 改動前的凍結基準
        cur = frozen if frozen is not None else sc[col].astype(float).values
        new = rolling_scores(pd.to_numeric(sc[rawcol], errors="coerce").values, w, high_ex)
        hyb = np.maximum(cur, new)

        def a(sel, lo, hi):
            return A.auc([sel[i] for i in ev if mask[i] and lo <= i < hi],
                         [sel[i] for i in nev if mask[i] and lo <= i < hi])
        c_tr, n_tr = a(cur, 0, split), a(hyb, 0, split)
        c_ho, n_ho = a(cur, split, n), a(hyb, split, n)
        f_cur = float((sc[col].where(mask) > 0).sum()) / max(int(mask.sum()), 1) * 100
        f_new = float(np.sum((hyb > 0) & mask)) / max(int(mask.sum()), 1) * 100
        # 取大值救不了「永遠滿分」的子項（max 恆等於絕對值）→ 另記純分位（取代）版
        r_tr, r_ho = a(new, 0, split), a(new, split, n)
        replace_rows.append((name, w, c_tr, r_tr, c_ho, r_ho, f_cur,
                             float(np.sum((new > 0) & mask)) / max(int(mask.sum()), 1) * 100))
        win_tr = (not np.isnan(n_tr)) and (not np.isnan(c_tr)) and n_tr > c_tr
        win_ho = (not np.isnan(n_ho)) and (not np.isnan(c_ho)) and n_ho > c_ho
        if win_tr and win_ho:
            vd = "✅ 採用"
            adopt.append((name, w, c_tr, n_tr, c_ho, n_ho))
        else:
            vd = "❌ 不採用（%s）" % ("僅 train 贏" if win_tr else "僅 holdout 贏" if win_ho else "兩邊都沒贏")
            reject.append((name, vd))
        print("%-18s%-5d%-11.3f%-11.3f%-11.3f%-11.3f%-9.1f%-9.1f  %s"
              % (name, w, c_tr, n_tr, c_ho, n_ho, f_cur, f_new, vd))

    print("\n採用 %d 項 / 不採用 %d 項" % (len(adopt), len(reject)))
    for name, w, c_tr, n_tr, c_ho, n_ho in adopt:
        print("  ✅ %-14s train %.3f→%.3f (%+.3f)｜holdout %.3f→%.3f (%+.3f)"
              % (name, c_tr, n_tr, n_tr - c_tr, c_ho, n_ho, n_ho - c_ho))
    for name, vd in reject:
        print("  ❌ %-14s %s" % (name, vd))

    print("")
    print("=" * 104)
    print("附表：純分位「取代」版（給取大值救不了的常亮／死項參考；未套用雙邊門檻）")
    print("=" * 104)
    print("%-16s%-5s%-10s%-10s%-10s%-10s%-9s%-9s" %
          ("子項", "配分", "現train", "取代train", "現hold", "取代hold", "現觸發", "取代觸發"))
    for name, w, c_tr, r_tr, c_ho, r_ho, f_cur, f_new in replace_rows:
        flag = "  ← 雙邊皆贏" if (r_tr > c_tr and r_ho > c_ho) else ""
        print("%-18s%-5d%-12.3f%-12.3f%-12.3f%-12.3f%-9.1f%-9.1f%s"
              % (name, w, c_tr, r_tr, c_ho, r_ho, f_cur, f_new, flag))


if __name__ == "__main__":
    main()
