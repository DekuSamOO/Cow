"""
tests/relative_low_backtest.py
相對底部（抄底）權重敏感度分析 — 分層 train/test
鏡像 tests/relative_high_backtest.py（逃頂版），方法論完全對稱。

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/relative_low_backtest.py

方法（對齊逃頂版已拍板決策，反向對稱）：
  1. 標記相對底部：日線 swing low(order=10)，其後 60 天內反彈 ≥18% 為正樣本。
  2. 僅對「有足夠歷史」的維度回測擬合：
       - 合約超冷（負資金費率年化，2021+，service/onchain fund_hist）
       - 技術回穩（底背離 RSI+MACD + RSI 超賣，全期，本檔 tech_low_score）
       - 情緒恐慌（F&G，2018+，alternative.me 全史）
       - 長週期深跌（Mayer/200週/冪律，全期日線，本檔 cycle_low_score）← 新第六維
     onchain(ETF/SOPR) / 總經 因無資料源或需發布行事曆 → **不擬合**，維持專家權重/灰燈。
  3. 樣本切分：時間序前半 train、後半 test（時序資料不洗牌，避免前視）。
  4. 對四個可擬合維度的「相對權重」做 grid search（步長 0.1、和=1，在 train 上最大化 AUC），
     在 test 上驗證 AUC，回報是否優於現行專家權重 → 據此決定六維配重。

AUC 以 Mann-Whitney U 實作（不引入 sklearn），與逃頂版同函式。
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
from core.divergence import detect_bottom_divergence
from core.relative_high import annualize_funding

RALLY_THRESH = 0.18      # 60 天內反彈門檻（對稱逃頂的 -0.18 回撤）
HORIZON = 60
ORDER = 10               # swing low 視窗（相對「重要」低點）
LOOKAHEAD_LABEL = 60


# ── AUC（Mann-Whitney U）— 與逃頂版同實作 ────────────────────────────────────
def auc(pos, neg):
    pos = [p for p in pos if p is not None and not np.isnan(p)]
    neg = [n for n in neg if n is not None and not np.isnan(n)]
    if not pos or not neg:
        return float("nan")
    allv = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    allv.sort(key=lambda x: x[0])
    ranks = {}
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        i = j + 1
    rank_sum_pos = sum(ranks[idx] for idx, (v, lab) in enumerate(allv) if lab == 1)
    n1, n0 = len(pos), len(neg)
    u = rank_sum_pos - n1 * (n1 + 1) / 2
    return u / (n1 * n0)


# ── 各維度子分數（底部側，鏡像 core/relative_high 反向；專家初版閾值，待擬合驗證）─────
def funding_low_score(ann):
    """合約超冷：資金費率年化負值越深分越高（max 20）。"""
    if ann is None or np.isnan(ann):
        return 0
    if   ann <= -50: return 20
    elif ann <= -30: return 16
    elif ann <= -15: return 12
    elif ann <= -5:  return 6
    elif ann < 0:    return 3
    return 0


def fng_low_score(v):
    """情緒恐慌：F&G 越低分越高（max 10）。"""
    if v is None or np.isnan(v):
        return 0
    if   v <= 10: return 10
    elif v <= 20: return 8
    elif v <= 25: return 5
    elif v <= 30: return 3
    return 0


def tech_low_score(row, df):
    """技術回穩：底背離 RSI+MACD combo(18) + RSI_14 超賣(7)（max 25）。"""
    rsi_div = detect_bottom_divergence(df, indicator="RSI_14")
    macd_col = "MACD" if "MACD" in df.columns else ("MACD_Hist" if "MACD_Hist" in df.columns else None)
    macd_div = detect_bottom_divergence(df, indicator=macd_col) if macd_col else {"has_divergence": False, "strength": 0.0}
    n = int(rsi_div["has_divergence"]) + int(macd_div["has_divergence"])
    strength = max(rsi_div.get("strength", 0.0), macd_div.get("strength", 0.0))
    if   n >= 2: d_s = 18
    elif n == 1: d_s = round(8 + 4 * strength)
    else:        d_s = 0

    rsi = row.get("RSI_14")
    if rsi is None or (isinstance(rsi, float) and np.isnan(rsi)):
        r_s = 0
    elif rsi <= 20: r_s = 7
    elif rsi <= 25: r_s = 5
    elif rsi <= 30: r_s = 3
    else:           r_s = 0
    return d_s + r_s


def cycle_low_score(row):
    """長週期深跌：Mayer(8) + 200週均比(7) + 冪律比(5)（max 20）。沿用 bear_bottom 底部分級。"""
    def _safe(v):
        return None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
    mayer = _safe(row.get("Mayer_Multiple"))
    sma200w = _safe(row.get("SMA200W_Ratio"))
    pl = _safe(row.get("PowerLaw_Ratio"))

    m_s = 0
    if mayer is not None:
        if   mayer < 0.8: m_s = 8
        elif mayer < 1.0: m_s = 5
        elif mayer < 1.2: m_s = 2
    s_s = 0
    if sma200w is not None:
        if   sma200w < 1.0: s_s = 7
        elif sma200w < 1.3: s_s = 5
        elif sma200w < 2.0: s_s = 2
    p_s = 0
    if pl is not None:
        if   pl < 2.0: p_s = 5
        elif pl < 5.0: p_s = 3
    return m_s + s_s + p_s


def main():
    print("載入資料 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)

    # 資金費率歷史（日均 8h%）
    _, _, fund = fetch_aux_history()
    fund_daily = pd.Series(dtype=float)
    if fund is not None and not fund.empty and "fundingRate" in fund.columns:
        f = fund.copy()
        if f.index.tz is not None:
            f.index = f.index.tz_localize(None)
        fund_daily = f["fundingRate"].resample("D").mean()

    # F&G 全史
    fng_map = {}
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=0&format=json",
                         timeout=20, verify=False)
        for it in r.json().get("data", []):
            d = pd.to_datetime(int(it["timestamp"]), unit="s").strftime("%Y-%m-%d")
            fng_map[d] = float(it["value"])
    except Exception as e:
        print("F&G 歷史抓取失敗：", e)

    # 標記相對底部：swing low + 其後 60 天反彈 >= 18%
    low = btc["low"].values.astype(float)
    close = btc["close"].values.astype(float)
    n = len(btc)
    bottoms = []
    for i in range(ORDER, n - ORDER):
        w = low[i - ORDER:i + ORDER + 1]
        if low[i] == np.nanmin(w) and (w == low[i]).sum() == 1:
            fut = close[i + 1:i + LOOKAHEAD_LABEL + 1]
            if len(fut) and (fut.max() / close[i] - 1) >= RALLY_THRESH:
                bottoms.append(i)

    # 可擬合集合：資金費率時代（2021+）
    fund_start = fund_daily.index.min() if not fund_daily.empty else pd.Timestamp("2021-01-01")
    fit_bottoms = [i for i in bottoms if btc.index[i] >= fund_start]
    print(f"全期相對底部 {len(bottoms)} 個；資金費率時代(>= {fund_start.date()}) {len(fit_bottoms)} 個（可擬合）")

    # 負樣本：距任何底 >= 45 天的隨機日（資金費率時代）
    rng = np.random.default_rng(42)
    era_idx = [k for k in range(n) if btc.index[k] >= fund_start and ORDER <= k < n - ORDER]
    def far_from_bottom(k):
        return all(abs(k - t) > 45 for t in fit_bottoms)
    neg_pool = [k for k in era_idx if far_from_bottom(k)]
    if not fit_bottoms or not neg_pool:
        print("樣本不足，無法擬合。"); return
    neg_idx = list(rng.choice(neg_pool, size=min(len(fit_bottoms) * 2, len(neg_pool)), replace=False))

    # 特徵：[funding/20, tech/25, fng/10, cycle/20]
    def feats_at(k):
        d = btc.index[k].strftime("%Y-%m-%d")
        ann = annualize_funding(float(fund_daily.get(btc.index[k].normalize(), np.nan))) \
            if not fund_daily.empty else None
        fs = funding_low_score(ann)
        tech = tech_low_score(btc.iloc[k], btc.iloc[:k + 1])
        gs = fng_low_score(fng_map.get(d, np.nan))
        cyc = cycle_low_score(btc.iloc[k])
        return fs, tech, gs, cyc

    P = np.array([feats_at(k) for k in fit_bottoms], float)
    N = np.array([feats_at(k) for k in neg_idx], float)

    names = ["合約超冷·負費率(0-20)", "技術回穩·底背離+超賣(0-25)", "F&G恐慌(0-10)", "長週期深跌(0-20)"]
    print("\n=== 各維度單獨判別力（AUC，>0.5 表示底部分數較高）===")
    for j, name in enumerate(names):
        a = auc(P[:, j].tolist(), N[:, j].tolist())
        print(f"  {name:30s} 底均 {P[:,j].mean():5.1f} / 非底均 {N[:,j].mean():5.1f}  AUC={a:.3f}")

    # 時間序切分 train/test
    order_pos = np.argsort([btc.index[k] for k in fit_bottoms])
    half = len(order_pos) // 2
    tr_p = P[order_pos[:half]]; te_p = P[order_pos[half:]]
    order_neg = np.argsort([btc.index[k] for k in neg_idx])
    hn = len(order_neg) // 2
    tr_n = N[order_neg[:hn]]; te_n = N[order_neg[hn:]]
    print(f"\n切分：train 底 {len(tr_p)}/非底 {len(tr_n)}；test 底 {len(te_p)}/非底 {len(te_n)}")

    maxes = np.array([20.0, 25.0, 10.0, 20.0])
    def composite(arr, w):
        return (arr / maxes) @ np.array(w)

    # grid search 四維相對權重（步長 0.1、和=1），最大化 train AUC
    best = None
    for a_ in range(0, 11):
        for b_ in range(0, 11 - a_):
            for c_ in range(0, 11 - a_ - b_):
                d_ = 10 - a_ - b_ - c_
                w = (a_ / 10, b_ / 10, c_ / 10, d_ / 10)
                tr_auc = auc(composite(tr_p, w).tolist(), composite(tr_n, w).tolist())
                if best is None or tr_auc > best[1]:
                    best = (w, tr_auc)
    w, tr_auc = best
    te_auc = auc(composite(te_p, w).tolist(), composite(te_n, w).tolist())

    # 現行專家相對權重（funding 25 / tech 20 / fng 15 / cycle 20 → 正規化，僅可擬合四維）
    exp_w = np.array([25, 20, 15, 20]) / 80.0
    exp_tr = auc(composite(tr_p, exp_w).tolist(), composite(tr_n, exp_w).tolist())
    exp_te = auc(composite(te_p, exp_w).tolist(), composite(te_n, exp_w).tolist())

    print("\n=== 權重 grid search（四可擬合維度相對權重 funding/tech/fng/cycle）===")
    print(f"  最佳(train)權重 = {w}  train AUC={tr_auc:.3f}  test AUC={te_auc:.3f}")
    print(f"  專家相對權重 (25/20/15/20) train AUC={exp_tr:.3f}  test AUC={exp_te:.3f}")
    # 把擬合相對權重換算回「可擬合維度合計 80 分」尺度，方便定案
    fit_total = 80
    scaled = tuple(round(x * fit_total) for x in w)
    print(f"  擬合權重換算到 {fit_total} 分尺度（funding/tech/fng/cycle）= {scaled}")
    print("\n結論：" + (
        "擬合權重在 test 上優於專家權重，建議採實證配重。" if te_auc > exp_te + 0.02 else
        "擬合權重未顯著優於專家權重（樣本小），維持專家配重較穩健。"))
    print("註：onchain(ETF/SOPR) / 總經未納入擬合（無資料源／需發布行事曆），維持灰燈/專家權重。")

    # ── 窗口式評估：底部附近 [-3,+20] 天窗口「最高複合分」是否能預警 ────────────
    print("\n=== 窗口式評估（底部前後 [-3,+20] 天取最高複合分，專家相對權重）===")
    def window_max_composite(center):
        lo, hi = max(0, center - 3), min(n - 1, center + 20)
        best_s = 0.0
        for k in range(lo, hi + 1):
            best_s = max(best_s, float(composite(np.array(feats_at(k), float), exp_w)))
        return best_s
    pos_win = [window_max_composite(k) for k in fit_bottoms]
    neg_win = [window_max_composite(k) for k in neg_idx]
    print(f"  窗口最高分 底均 {np.mean(pos_win):.3f} / 非底均 {np.mean(neg_win):.3f}  AUC={auc(pos_win, neg_win):.3f}")
    for thr in (0.35, 0.45, 0.55):
        hit = np.mean([s >= thr for s in pos_win]) * 100
        fp  = np.mean([s >= thr for s in neg_win]) * 100
        print(f"  門檻 {thr:.2f}: 底部命中率 {hit:.0f}%  非底誤報率 {fp:.0f}%")
    print("  （僅含負費率/技術/F&G/長週期 四維；實際系統再疊加 onchain/總經，僅作灰燈展示）")

    # ── 未擬合兩維（onchain / macro）驗證（2026-06-16；專家配重 + 敏感度）──────────
    validate_unfitted_dims(btc, fit_bottoms, neg_idx, fund_daily, fng_map)


def _load_sopr_cache():
    """讀 committed db/bottom_metrics_cache.json 的 SOPR 歷史（避免再打 bitcoin-data 觸發 429）。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "db", "bottom_metrics_cache.json")
    try:
        import json
        c = json.load(open(p, encoding="utf-8"))
        return {d: float(v) for d, v in (c.get("sopr") or {}).items()}
    except Exception as e:
        print("  SOPR 快取讀取失敗：", e)
        return {}


def validate_unfitted_dims(btc, fit_bottoms, neg_idx, fund_daily, fng_map):
    """
    抄底雷達兩處未擬合維度（onchain=鏈上吸籌、macro=總經順風）的「專家配重 + 敏感度」驗證。

    方法（資料受限下能做的最誠實驗證）：
      onchain：直接呼叫真正的 compute_relative_low_score，餵歷史 SOPR（committed 快取，
               2022-06+），比較
                 (1) onchain 子分數單維 AUC（方向是否正確：底部 SOPR 子分應較高）
                 (2) 合成分數「加 onchain vs 不加」的 AUC（增量是否無害/有益）
                 (3) onchain 權重擾動（×0 / ×0.5 / ×1 / ×1.5）下合成 AUC 與門檻命中穩定性
               ETF：歷史僅 2024+ 且無逐日歷史快照 → 標資料不足，僅現役方向檢查（不納 AUC）。
      macro ：無歷史事件 flag 源 → 回測樣本中 macro 子分恆 0，無法影響歷史判別；
               其 live 值為「event-window 規則式」(deterministic by design)，非統計可擬合。
    結論據此決定是否移除 core/relative_low.UNFITTED_DIMS_LOW 標記。
    """
    from core.relative_low import compute_relative_low_score

    print("\n" + "=" * 72)
    print("未擬合兩維驗證（onchain / macro）— 專家配重 + 敏感度")
    print("=" * 72)

    sopr_map = _load_sopr_cache()
    if not sopr_map:
        print("無 SOPR 快取 → onchain 無法驗證，維持 UNFITTED。")
        return
    sopr_start = pd.Timestamp(min(sopr_map))
    print(f"SOPR 歷史：n={len(sopr_map)}，{min(sopr_map)} ~ {max(sopr_map)}")

    def sample(k):
        """回傳 (合成分_with_onchain, onchain子分, 是否在SOPR期內)。onchain 僅 SOPR（etf=None→0）。"""
        d = btc.index[k]
        ds = d.strftime("%Y-%m-%d")
        f8 = None
        if not fund_daily.empty:
            fv = float(fund_daily.get(d.normalize(), np.nan))
            f8 = None if np.isnan(fv) else fv
        fng_val = fng_map.get(ds)
        in_sopr = d >= sopr_start
        sopr_val = sopr_map.get(ds) if in_sopr else None
        sc, sig = compute_relative_low_score(btc.iloc[k], btc.iloc[:k + 1],
                                             funding_8h=f8, fng=fng_val, sopr=sopr_val)
        return sc, sig["onchain"]["score"], in_sopr

    pos = [sample(k) for k in fit_bottoms]
    neg = [sample(k) for k in neg_idx]
    # 僅 SOPR 期內樣本做 onchain 比較（公平）
    pos_s = [(sc, oc) for sc, oc, ok in pos if ok]
    neg_s = [(sc, oc) for sc, oc, ok in neg if ok]
    print(f"SOPR 期內樣本：底 {len(pos_s)} / 非底 {len(neg_s)}")
    if len(pos_s) < 4 or len(neg_s) < 4:
        print("SOPR 期內樣本過少（<4），onchain 驗證不可靠 → 維持 UNFITTED。")
        macro_note()
        return

    # (1) onchain 子分數單維 AUC（方向）
    oc_auc = auc([o for _, o in pos_s], [o for _, o in neg_s])
    print(f"\n[onchain 方向] SOPR 子分 底均 {np.mean([o for _,o in pos_s]):.2f} / "
          f"非底均 {np.mean([o for _,o in neg_s]):.2f}  AUC={oc_auc:.3f}  "
          f"（>0.5＝底部 SOPR 子分較高，方向正確）")

    # (2) 合成分數「加 onchain vs 不加」AUC（onchain 子分僅 SOPR，故 without = with − onchain子分）
    with_auc = auc([sc for sc, _ in pos_s], [sc for sc, _ in neg_s])
    wo_auc = auc([sc - o for sc, o in pos_s], [sc - o for sc, o in neg_s])
    print(f"[onchain 增量] 合成 AUC：加 onchain {with_auc:.3f} vs 不加 {wo_auc:.3f}  "
          f"Δ={with_auc - wo_auc:+.3f}（≥-0.005＝無害）")

    # (3) onchain 權重擾動（子分 × factor），看 AUC 與門檻 60 命中穩定
    print("[onchain 擾動] onchain 權重 ×factor 下合成 AUC / 門檻60底部命中率：")
    for fac in (0.0, 0.5, 1.0, 1.5):
        pv = [sc - o + o * fac for sc, o in pos_s]
        nv = [sc - o + o * fac for sc, o in neg_s]
        a = auc(pv, nv)
        hit = np.mean([v >= 60 for v in pv]) * 100
        print(f"    ×{fac:<3}  AUC={a:.3f}  底部≥60 命中 {hit:.0f}%")

    macro_note()

    # ── 結論判定 ────────────────────────────────────────────────────────────────
    onchain_ok = (oc_auc > 0.5) and (with_auc - wo_auc >= -0.005)
    print("\n" + "-" * 72)
    print("結論：")
    print(f"  onchain（鏈上吸籌/SOPR）：方向 {'正確' if oc_auc > 0.5 else '不正確'}"
          f"（AUC {oc_auc:.3f}）、增量 {'無害' if with_auc - wo_auc >= -0.005 else '有害'}"
          f"（Δ {with_auc - wo_auc:+.3f}）→ "
          f"{'可移除 UNFITTED（標 SOPR 方向驗證 2026-06）' if onchain_ok else '維持 UNFITTED'}")
    print("  macro（總經順風）：無歷史事件源、回測恆 0 不影響判別；live 為 event-window 規則式，"
          "敏感度上對歷史合成無害 → 由規則正確性背書（非統計擬合）。")
    print("  → 建議：onchain 依驗證結果處置；macro 改標『規則式/敏感度檢核』而非『無資料源』。")


def macro_note():
    print("\n[macro] 本檔回測樣本 macro=None（不打 FRED）→ macro 子分恆 0、對本檔 AUC 無影響。"
          "\n        dovish/hawkish flags 的 FRED point-in-time 回測已獨立完成於"
          "\n        tests/relative_low_macro_backtest.py（2026-07，家用網路 FRED 可達）："
          "\n          抄底 dovish：全期 AUC 0.448（方向反）/ 費率era 0.562（弱）→ 落後確認、維持低權規則式；"
          "\n          逃頂 hawkish：全期 AUC 0.607 / 費率era 0.660 → 頂部與升息環境同步，方向明確有效。")


if __name__ == "__main__":
    main()
