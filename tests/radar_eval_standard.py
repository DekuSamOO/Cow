"""
tests/radar_eval_standard.py
逃頂／抄底雷達評估標準 — **單一真實來源**（2026-08-26 立）

規格正本：vault `Github\\Cow\\雷達評估標準.md`。本檔是它的可執行版本。
任何評估腳本一律 import 這裡的常數與函式，**不要各自複製一份 0.18**——
三套回測腳本抄同一個固定門檻、卻在三個波動天差地遠的市場上使用，正是本標準要修的問題。

核心：事件門檻改用「該標的自身的波動倍數」，precision 一律附隨機基準率。
"""
import numpy as np
import pandas as pd

# ── 事件定義（規格 No.2）────────────────────────────────────────────────────
ORDER = 10              # swing 視窗（±交易日），沿用既有腳本，改了舊結論全部不可比
H = 60                  # 前瞻視窗（交易日）
VOL_LOOKBACK = 252      # 估波動的回看視窗
K_TOP = 1.30            # 頂側倍數 → 跨標的中位 1.55 次/年
K_BOT = 1.90            # 底側倍數 → 跨標的中位 1.50 次/年（底側較鬆是因標的長期向上漂移）

# ── 決策視窗（規格 No.4）────────────────────────────────────────────────────
LEAD = 30               # 訊號要在事件前 LEAD 日內響過才算抓到
GRACE = 7               # 底側容許遲到（買晚一點可以，買早了就套）；頂側為 0


def realized_sigma_h(close: pd.Series, h: int = H, lookback: int = VOL_LOOKBACK) -> pd.Series:
    """PiT 的 h 日已實現波動（比例）。只用 t 之前的資料，無前視。"""
    r = np.log(close).diff()
    return r.rolling(lookback).std() * np.sqrt(h)


def swing_events(close, is_top: bool, sigma_h=None, k=None,
                 fixed_move=None, order: int = ORDER, horizon: int = H) -> list:
    """swing 極值 + 其後 horizon 日反向幅度達標 → 事件索引清單。

    sigma_h + k：波動標準化門檻（**本標準的正式作法**）。
    fixed_move ：固定百分比門檻，僅供與舊口徑對拍用，**不得作為新結論的依據**。
    """
    v = np.asarray(close, float)
    n = len(v)
    if fixed_move is not None:
        thr = np.full(n, float(fixed_move))
    else:
        # 展開寫，勿改回巢狀三元式：`K_TOP if is_top else K_BOT if k is None else k`
        # 會被解析成 `K_TOP if is_top else (K_BOT if k is None else k)`，
        # is_top=True 時**傳入的 k 會被整個忽略**（2026-08-26 實測：k 從 1.3 掃到 3.5，
        # 頂側事件數紋風不動 9 次，就是這個優先序）。
        if k is None:
            kk = K_TOP if is_top else K_BOT
        else:
            kk = float(k)
        thr = np.asarray(sigma_h, float) * kk
    out = []
    for i in range(order, n - order):
        t = thr[i]
        if not np.isfinite(t) or t <= 0:
            continue
        w = v[i - order:i + order + 1]
        if (v[i] != w.max()) if is_top else (v[i] != w.min()):
            continue
        fwd = v[i + 1:min(i + 1 + horizon, n)]
        if len(fwd) == 0:
            continue
        mv = (fwd.min() / v[i] - 1) if is_top else (fwd.max() / v[i] - 1)
        if (mv <= -t) if is_top else (mv >= t):
            out.append(i)
    return out


def event_window_mask(events: list, n: int, is_top: bool) -> np.ndarray:
    """事件視窗布林遮罩：頂 [e-LEAD, e]、底 [e-LEAD, e+GRACE]。"""
    m = np.zeros(n, bool)
    g = 0 if is_top else GRACE
    for e in events:
        m[max(0, e - LEAD):min(n, e + g + 1)] = True
    return m


def base_rate(events: list, n: int, is_top: bool) -> float:
    """**隨機訊號的 precision**＝事件視窗覆蓋全樣本的比例。

    沒有這個數字，precision 不能解讀：BTC 底側基準是 66%、SPY 底側是 10%，
    同樣報「precision 68%」，一個比亂猜差、一個是強訊號。
    """
    if not events or n == 0:
        return float("nan")
    return float(event_window_mask(events, n, is_top).mean())


def threshold_metrics(score: np.ndarray, events: list, is_top: bool, thresholds) -> list:
    """每個門檻的決策品質 → [(門檻, 觸發日數, precision, lift, recall, 中位提前, 抓到數, 事件數)]。"""
    score = np.asarray(score, float)
    n = len(score)
    br = base_rate(events, n, is_top)
    win = event_window_mask(events, n, is_top)
    ev = np.asarray(events, int)
    out = []
    for t in thresholds:
        fired = score >= t
        n_fire = int(np.nansum(fired))
        if n_fire == 0 or len(ev) == 0:
            out.append((t, n_fire, np.nan, np.nan, np.nan, np.nan, 0, len(ev)))
            continue
        prec = float(win[fired].mean())
        lift = prec / br if br else np.nan
        caught, leads = 0, []
        g = 0 if is_top else GRACE
        idx = np.arange(n)
        for e in ev:
            m = fired & (idx >= e - LEAD) & (idx <= e + g)
            if m.any():
                caught += 1
                leads.append(int(e - idx[m].min()))
        rec = caught / len(ev)
        out.append((t, n_fire, prec, lift, rec,
                    float(np.median(leads)) if leads else np.nan, caught, len(ev)))
    return out


def auc(pos, neg) -> float:
    """Mann-Whitney AUC（處理同分：midrank）。沿用 radar_subitem_audit 的實作語意。"""
    pos = [p for p in pos if p is not None and np.isfinite(p)]
    neg = [x for x in neg if x is not None and np.isfinite(x)]
    if not pos or not neg:
        return float("nan")
    a = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg], key=lambda x: x[0])
    ranks, i = {}, 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[j + 1][0] == a[i][0]:
            j += 1
        for kk in range(i, j + 1):
            ranks[kk] = (i + j) / 2 + 1
        i = j + 1
    rs = sum(ranks[kk] for kk, (v, lbl) in enumerate(a) if lbl == 1)
    return (rs - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def print_threshold_table(name, score, events, is_top, thresholds, indent="  "):
    """統一的門檻表輸出格式（跨資產類別一致，方便對拍）。"""
    n = len(score)
    br = base_rate(events, n, is_top)
    print("%s%s　事件 %d 次｜**隨機基準 precision = %.0f%%**"
          % (indent, name, len(events), br * 100))
    print("%s%-6s %-9s %-11s %-8s %-13s %s"
          % (indent, "門檻", "觸發日數", "precision", "lift", "recall", "中位提前"))
    rows = []
    for t, nf, prec, lift, rec, lead, caught, n_ev in threshold_metrics(
            score, events, is_top, thresholds):
        print("%s%-6d %-9d %-11s %-8s %-13s %s"
              % (indent, t, nf,
                 "—" if not np.isfinite(prec) else "%.0f%%" % (prec * 100),
                 "—" if not np.isfinite(lift) else "%.2fx" % lift,
                 "—" if not np.isfinite(rec) else "%.0f%% (%d/%d)" % (rec * 100, caught, n_ev),
                 "—" if not np.isfinite(lead) else "%.0f 日" % lead))
        rows.append({"threshold": t, "n_fire": nf,
                     "precision": None if not np.isfinite(prec) else round(prec, 4),
                     "lift": None if not np.isfinite(lift) else round(lift, 3),
                     "recall": None if not np.isfinite(rec) else round(rec, 4),
                     "caught": caught, "n_events": n_ev,
                     "median_lead": None if not np.isfinite(lead) else lead})
    return {"base_rate": None if not np.isfinite(br) else round(br, 4), "rows": rows}
