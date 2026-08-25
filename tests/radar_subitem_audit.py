"""
tests/radar_subitem_audit.py
逃頂／抄底雷達「全子項體檢」（2026-08-25）— 找出資金費率那種「有欄位但實際拿不到分」的項目。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/radar_subitem_audit.py

背景：2026-08-25 發現逃頂 funding 子項（20 分）因幣安 624 日未越利率基準而恆為 0 分。
      本腳本把同一套體檢套到**所有**子項，三欄判定：
        可得性  runtime 到底拿不拿得到輸入（拿不到＝配分是虛的）
        觸發率  歷史上有多少比例的日子真的拿到 >0 分（0% ＝死項；100% ＝常亮，同樣無鑑別力）
        AUC     該子項分數對真頂/真底的單維鑑別力（0.5 ＝無訊號）

方法：**逐日重放正式計分函數本身**（core._score_*），不重寫一套階梯——重寫等於量到副本
      而不是產品。獨立性由外部 agent 覆核負責
      （agent 定義在使用者家目錄：~/.claude/agents/independent-auditor.md，不在本 repo 內）。
      頂/底樣本與 AUC 實作沿用 funding_threshold_calib 的既有方法論（H=60、ORDER=10、MOVE=0.18）。

已知不可測（誠實列出，不假裝有數字）：
  OI 分位 / OI 1h 清洗  — 幣安 openInterestHist 只回近 30 日，無長史
  BTC.D                 — 本機 market_snapshot 僅 108 日
  macro hawkish/dovish  — FRED 被公司網路 SSL 攔截，runtime 根本拿不到（見報告「死項」段）
  macro 事件臨近        — 只有當下的 Notion 行事曆快照，無歷史
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.indicators import calculate_technical_indicators
from core.bear_bottom import calculate_bear_bottom_indicators
from core.relative_high import (_score_derivatives, _score_technical, _score_onchain,
                                _score_sentiment)
from core.relative_low import (_score_derivatives_low, _score_technical_low,
                               _score_onchain_low, _score_sentiment_low, _score_cycle)
from service.etf_flow import _summarize
from service.funding_history import load_funding_ann_daily

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H, ORDER, MOVE = 60, 10, 0.18
TRAIN_END = "2024-01-01"
START = "2019-09-10"          # 資費史起點（更早的日子多數子項無輸入）


def auc(pos, neg):
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    a = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg], key=lambda x: x[0])
    ranks, i = {}, 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[j + 1][0] == a[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    rs = sum(ranks[k] for k, (v, lbl) in enumerate(a) if lbl == 1)
    return (rs - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _jload(name):
    with open(os.path.join(ROOT, "db", name), encoding="utf-8") as f:
        return json.load(f)


def load_all():
    btc = pd.read_csv(os.path.join(ROOT, "db", "cache", "BTC_HISTORY.csv"),
                      index_col=0, parse_dates=True)
    btc = calculate_bear_bottom_indicators(calculate_technical_indicators(btc))
    fund = load_funding_ann_daily(refresh=False)
    bm = _jload("bottom_metrics_cache.json")
    etf = _jload("etf_flow.json")["data"]
    fng_path = os.path.join(ROOT, "db", "cache", "fng_history.json")
    fng = json.load(open(fng_path, encoding="utf-8")) if os.path.exists(fng_path) else {}
    return btc, fund, bm.get("mvrv_zscore", {}), bm.get("sopr", {}), etf, fng


def build_scores():
    """逐日呼叫正式計分函數 → 每個子項一條分數序列。"""
    btc, fund, mvrv, sopr, etf, fng = load_all()
    idx = btc.index[(btc.index >= START)]
    etf_dates = sorted(etf)
    rows = []
    for d in idx:
        key = d.strftime("%Y-%m-%d")
        i = btc.index.get_loc(d)
        row, sub_df = btc.iloc[i], btc.iloc[:i + 1]
        f8h = None
        if key in fund.index.strftime("%Y-%m-%d"):
            f8h = float(fund.loc[d]) / 1095 if d in fund.index else None
        hist = [float(v) for v in fund[fund.index <= d].tail(180).values] or None
        etf_pit = {k: v for k, v in etf.items() if k <= key}
        etf_sum = _summarize(etf_pit) if etf_pit else None
        mv = mvrv.get(key)
        sp = sopr.get(key)
        fg = fng.get(key)

        top_d = _score_derivatives(f8h, None, hist)["sub"]
        top_t = _score_technical(row, sub_df)["sub"]
        top_o = _score_onchain(etf_sum, sp, mv)["sub"]
        top_s = _score_sentiment(fg, None)["sub"]
        low_d = _score_derivatives_low(f8h, None, hist)["sub"]
        low_t = _score_technical_low(row, sub_df)["sub"]
        low_o = _score_onchain_low(etf_sum, sp, mv)["sub"]
        low_s = _score_sentiment_low(fg, None)["sub"]
        low_c = _score_cycle(row)["sub"]
        rows.append({
            "date": d,
            "T_funding": top_d.get("funding_score_eff"), "T_div": top_t.get("divergence_score"),
            "T_rsi": top_t.get("rsi_score"), "T_etf": top_o.get("etf_score"),
            "T_sopr": top_o.get("sopr_score"), "T_mvrv": top_o.get("mvrv_z_score"),
            "T_fng": top_s.get("fng_score"),
            "L_funding": low_d.get("funding_score"), "L_div": low_t.get("divergence_score"),
            "L_rsi": low_t.get("rsi_score"), "L_etf": low_o.get("etf_score"),
            "L_sopr": low_o.get("sopr_score"), "L_mvrv": low_o.get("mvrv_z_score"),
            "L_fng": low_s.get("fng_score"), "L_mayer": low_c.get("mayer_score"),
            "L_sma200w": low_c.get("sma200w_score"), "L_powerlaw": low_c.get("powerlaw_score"),
            "has_etf": bool(etf_sum and etf_sum.get("n")), "has_mvrv": mv is not None,
            "has_sopr": sp is not None, "has_fng": fg is not None,
            "has_fund": f8h is not None,
            # 原始指標值（用來算「階梯保留了原始指標多少鑑別力」）
            "raw_mvrv": mv, "raw_sopr": sp, "raw_fng": fg,
            "raw_rsi": row.get("RSI_14"), "raw_mayer": row.get("Mayer_Multiple"),
            "raw_sma200w": row.get("SMA200W_Ratio"), "raw_powerlaw": row.get("PowerLaw_Ratio"),
            "raw_fund_ann": top_d.get("funding_ann"),
            "raw_etf_out": (etf_sum or {}).get("consecutive_outflow_days"),
            "raw_etf_in": (etf_sum or {}).get("consecutive_inflow_days"),
        })
    return btc.loc[idx], pd.DataFrame(rows).set_index("date")


def swings(series, close, n, is_top):
    out = []
    for i in range(ORDER, n - ORDER):
        w = series[i - ORDER:i + ORDER + 1]
        ext = (series[i] == np.nanmax(w)) if is_top else (series[i] == np.nanmin(w))
        if not ext:
            continue
        fut = close[i + 1:i + H + 1]
        # 未來窗不足 H 日就不標記：序列末端的樣本會用「不完整的視窗」判正負樣本
        # （2026-08-25 獨立檢核 🟠 No.4：holdout 16 個正樣本有 3 個只有 11~55 日窗）
        if len(fut) < H:
            continue
        mv = (fut.min() / close[i] - 1) if is_top else (fut.max() / close[i] - 1)
        if (mv <= -MOVE) if is_top else (mv >= MOVE):
            out.append(i)
    return out


# (欄位, 顯示名, 配分, 是頂側?, 可測旗標欄)
ITEMS = [
    ("T_funding", "逃頂 資金費率",   20, True,  "has_fund"),
    ("T_div",     "逃頂 頂背離",     18, True,  None),
    ("T_rsi",     "逃頂 RSI 超買",    7, True,  None),
    ("T_etf",     "逃頂 ETF 流出",   12, True,  "has_etf"),
    ("T_sopr",    "逃頂 SOPR 飆高",   8, True,  "has_sopr"),
    ("T_mvrv",    "逃頂 MVRV-Z",      6, True,  "has_mvrv"),
    ("T_fng",     "逃頂 F&G 貪婪",   10, True,  "has_fng"),
    ("L_mayer",   "抄底 Mayer",      10, False, None),
    ("L_sma200w", "抄底 200週均",     9, False, None),
    ("L_powerlaw", "抄底 冪律",       6, False, None),
    ("L_funding", "抄底 負費率",     10, False, "has_fund"),
    ("L_div",     "抄底 底背離",     14, False, None),
    ("L_rsi",     "抄底 RSI 超賣",    6, False, None),
    ("L_etf",     "抄底 ETF 流入",    6, False, "has_etf"),
    ("L_sopr",    "抄底 SOPR 割肉",   4, False, "has_sopr"),
    ("L_mvrv",    "抄底 MVRV-Z",      6, False, "has_mvrv"),
    ("L_fng",     "抄底 F&G 恐懼",   10, False, "has_fng"),
]

UNTESTABLE = [
    ("逃頂 OI 分位", 10, "幣安 openInterestHist 只回近 30 日 → 無長史可測"),
    ("抄底 OI 1h 清洗", 10, "同上"),
    ("逃頂 BTC.D 輪動", 5, "本機 market_snapshot 僅 108 日"),
    ("抄底 BTC.D 上升", 5, "同上"),
    ("逃頂 macro hawkish", 7, "FRED 被公司網路 SSL 攔截 → runtime 恆為 None（死項）"),
    ("抄底 macro dovish", 7, "同上（死項）"),
    ("逃頂/抄底 macro 事件臨近", 3, "只有當下 Notion 行事曆快照，無歷史"),
]


def main():
    print("載入與逐日重放（呼叫正式計分函數）…")
    btc, sc = build_scores()
    close = btc["close"].values
    high, low = btc["high"].values, btc["low"].values
    n = len(btc)
    tops = swings(high, close, n, True)
    bots = swings(low, close, n, False)
    rng = np.random.default_rng(0)
    rr = range(ORDER, n - ORDER)
    ntp = list(rng.choice([k for k in rr if all(abs(k - t) > 30 for t in tops)],
                          size=min(len(tops) * 3, n), replace=False))
    nbt = list(rng.choice([k for k in rr if all(abs(k - t) > 30 for t in bots)],
                          size=min(len(bots) * 3, n), replace=False))
    split_i = int(np.searchsorted(btc.index.values, np.datetime64(TRAIN_END)))
    print(f"樣本 {n} 日 {btc.index[0].date()}~{btc.index[-1].date()}｜"
          f"頂 {len(tops)}／底 {len(bots)}｜train/holdout 切點 {TRAIN_END}")

    print("\n" + "=" * 104)
    print(f"{'子項':<18}{'配分':>4}{'可測日數':>9}{'n頂/底':>8}{'n對照':>7}{'觸發率':>8}{'滿分率':>8}"
          f"{'AUC全期':>9}{'AUC-train':>10}{'AUC-hold':>9}  判定")
    print("=" * 104)
    verdicts = []
    for col, name, w, is_top, flagcol in ITEMS:
        s = sc[col].astype(float)
        mask = sc[flagcol].values if flagcol else np.ones(len(sc), bool)
        avail = s.where(mask)
        n_ok = int(mask.sum())
        fire = float((avail > 0).sum()) / max(n_ok, 1) * 100
        full = float((avail >= w).sum()) / max(n_ok, 1) * 100
        mean = float(avail.mean()) if n_ok else float("nan")
        ev, nev = (tops, ntp) if is_top else (bots, nbt)
        vals = s.values
        a_all = auc([vals[i] for i in ev if mask[i]], [vals[i] for i in nev if mask[i]])
        a_tr = auc([vals[i] for i in ev if mask[i] and i < split_i],
                   [vals[i] for i in nev if mask[i] and i < split_i])
        a_ho = auc([vals[i] for i in ev if mask[i] and i >= split_i],
                   [vals[i] for i in nev if mask[i] and i >= split_i])
        n_pos = sum(1 for i in ev if mask[i])
        n_neg = sum(1 for i in nev if mask[i])
        # 樣本太少時 AUC 幾乎是雜訊（n_pos<15 → SE 已達 ±0.09 量級）→ 不下「方向相反/弱」的定罪判詞
        thin = n_pos < 15
        if fire < 1:
            vd = "❌ 死項（幾乎不觸發）"
        elif fire > 95:
            vd = "⚠️ 常亮（無鑑別力）"
        elif thin:
            vd = f"❔ 樣本不足(n={n_pos})不判"
        elif not np.isnan(a_all) and a_all < 0.5:
            vd = "❌ 方向相反"
        elif not np.isnan(a_all) and a_all < 0.55:
            vd = "⚠️ 弱（AUC<0.55）"
        else:
            vd = "✅ 健康"
        verdicts.append((name, w, fire, a_all, a_tr, a_ho, vd, n_pos))
        print(f"{name:<20}{w:>4}{n_ok:>9}{n_pos:>8}{n_neg:>7}{fire:>7.1f}%{full:>7.1f}%"
              f"{a_all:>9.3f}{a_tr:>10.3f}{a_ho:>9.3f}  {vd}")

    # ── 階梯保真度：原始指標 AUC vs 階梯化後 AUC ────────────────────────────────
    # 倉庫的子項驗證多半是拿**原始指標**跑 AUC，但上線的是**階梯化整數分**。
    # 兩者落差大＝門檻訂在測期間幾乎碰不到的位置，驗證結論沒有轉移到實作。
    # （逃頂 MVRV-Z 就是此例：原始 z 值 AUC 0.592 已 committed，階梯 ≥3/≥5/≥7 只有 2.2% 日子觸發。）
    print("\n" + "=" * 104)
    print("階梯保真度：原始指標 vs 上線階梯（差距大＝門檻與測期間分布錯位）")
    print("=" * 104)
    print(f"{'子項':<18}{'原始指標AUC':>12}{'階梯後AUC':>11}{'落差':>9}{'觸發率':>9}  判定")
    RAW = [
        ("T_mvrv", "raw_mvrv", "逃頂 MVRV-Z", True, 1, "has_mvrv"),
        ("T_sopr", "raw_sopr", "逃頂 SOPR", True, 1, "has_sopr"),
        ("T_fng", "raw_fng", "逃頂 F&G", True, 1, "has_fng"),
        ("T_rsi", "raw_rsi", "逃頂 RSI", True, 1, None),
        ("T_funding", "raw_fund_ann", "逃頂 資金費率", True, 1, "has_fund"),
        ("T_etf", "raw_etf_out", "逃頂 ETF 流出", True, 1, "has_etf"),
        ("L_mvrv", "raw_mvrv", "抄底 MVRV-Z", False, -1, "has_mvrv"),
        ("L_sopr", "raw_sopr", "抄底 SOPR", False, -1, "has_sopr"),
        ("L_fng", "raw_fng", "抄底 F&G", False, -1, "has_fng"),
        ("L_rsi", "raw_rsi", "抄底 RSI", False, -1, None),
        ("L_funding", "raw_fund_ann", "抄底 負費率", False, -1, "has_fund"),
        ("L_mayer", "raw_mayer", "抄底 Mayer", False, -1, None),
        ("L_sma200w", "raw_sma200w", "抄底 200週均", False, -1, None),
        ("L_powerlaw", "raw_powerlaw", "抄底 冪律", False, -1, None),
        ("L_etf", "raw_etf_in", "抄底 ETF 流入", False, 1, "has_etf"),
    ]
    for col, rawcol, name, is_top, sign, flagcol in RAW:
        mask = sc[flagcol].values if flagcol else np.ones(len(sc), bool)
        ev, nev = (tops, ntp) if is_top else (bots, nbt)
        raw = pd.to_numeric(sc[rawcol], errors="coerce").values * sign
        lad = sc[col].astype(float).values
        a_raw = auc([raw[i] for i in ev if mask[i]], [raw[i] for i in nev if mask[i]])
        a_lad = auc([lad[i] for i in ev if mask[i]], [lad[i] for i in nev if mask[i]])
        fire = float((sc[col].where(mask) > 0).sum()) / max(int(mask.sum()), 1) * 100
        gap = a_raw - a_lad
        vd = ("❌ 階梯吃掉訊號" if gap > 0.06 else
              "⚠️ 階梯略損" if gap > 0.03 else
              "✅ 階梯保真")
        print(f"{name:<20}{a_raw:>12.3f}{a_lad:>11.3f}{gap:>+9.3f}{fire:>8.1f}%  {vd}")

    print("\n" + "=" * 104)
    print("無歷史可測（不假裝有數字）")
    print("=" * 96)
    for name, w, why in UNTESTABLE:
        print(f"  {name:<26}{w:>3} 分   {why}")

    print("\n" + "=" * 96)
    print("配分 vs AUC 對照（AUC 越高越該給分；配分明顯與鑑別力不匹配者列出）")
    print("=" * 96)
    ok = [v for v in verdicts if not np.isnan(v[3])]
    for side, label in ((True, "逃頂"), (False, "抄底")):
        sub = [v for v in ok if v[0].startswith(label)]
        sub.sort(key=lambda x: -x[3])
        print(f"\n  {label}側 依 AUC 由高到低：")
        for name, w, fire, a_all, a_tr, a_ho, vd, n_pos in sub:
            print(f"    {a_all:.3f}  {name:<18}現配分 {w:>2} 分  n={n_pos:<3} {vd}")


if __name__ == "__main__":
    main()
