"""
tests/relative_low_macro_backtest.py
總經 dovish/hawkish flags 回測 — 完成「待 FRED 歷史回補後回測」的未擬合子維。

背景
----
core/relative_low._score_macro_low 的「dovish flags（通膨/就業）」與
core/relative_high._score_macro 的「hawkish flags（通膨/就業）」原標記為
PENDING_FIT_SUBDIMS_LOW（可擬合，但公司網路 FRED 被擋 → 無歷史源無法回測）。
本腳本在「家用/雲端網路（FRED 可達）」下，用 FRED 月頻歷史建立 **point-in-time**
（含發布延遲，無前視）的 dovish/hawkish flag 序列，驗證其對「相對底部 / 相對頂部」
的判別力（單維 AUC + 增量 AUC），據此決定該子維屬於：
  (a) 可採實證權重（AUC 明顯 >0.55、增量無害）
  (b) 弱維（0.5<AUC<0.55，給低權僅參考）
  (c) 維持規則式/灰燈（AUC≈0.5 或方向錯）

方法（鏡像 tests/relative_low_backtest.py，反向對稱）
----------------------------------------------------
1. BTC 日線 swing low(order=10) + 其後 60 天反彈≥18% → 相對底部正樣本；
   swing high + 其後 60 天回撤≥18% → 相對頂部正樣本。
2. 負樣本：距任一同向轉折 >45 天的隨機日。
3. FRED 月頻：CPIAUCSL / PCEPI / PAYEMS / UNRATE，計算 YoY 與 MoM，依「升溫/降溫、
   就業強/弱」轉 bool flag（與 service/macro_data 的 trend 判定同閾值）。
4. **point-in-time**：每筆月度觀測掛一個 available_date = observation_date + 發布延遲，
   評估某日 D 時只取 available_date ≤ D 的最近一筆 → 無前視。
5. dovish_score = (通膨降溫?4:0)+(就業轉弱?3:0)，cap 7（同 _score_macro_low）。
   hawkish_score = (通膨升溫?4:0)+(就業強勁?3:0)，cap 7（同 _score_macro）。
6. 單維 AUC（全期 + 資金費率時代）；增量：把 macro 疊到既有可擬合複合分後 test AUC 變化。

手動執行（非 pytest，需 FRED 可達的網路）：
  <python> tests/relative_low_macro_backtest.py
"""
import sys, os, io
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
from core.relative_high import annualize_funding

# 既有抄底回測的標記/子分函式（單一真實來源，避免漂移）
from tests.relative_low_backtest import (
    auc, funding_low_score, fng_low_score, tech_low_score, cycle_low_score,
    RALLY_THRESH, ORDER, LOOKAHEAD_LABEL,
)

_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"

# 各序列自 observation_date（FRED 標記為參考月 1 號）起算的「實際發布延遲」（天）。
# 取保守上界，確保評估日 D 不會用到尚未公布的數據（無前視）。
#   CPI    參考月資料約次月中旬發布 → 月初 +45 天足夠
#   PCE    約次月底發布            → 月初 +60 天
#   PAYEMS/UNRATE（非農+失業率）約次月第一個週五 → 月初 +40 天
_PUB_LAG_DAYS = {"CPIAUCSL": 45, "PCEPI": 60, "PAYEMS": 40, "UNRATE": 40}

TREND_EPS = 0.15   # YoY 升/降溫門檻（pp），同 service/macro_data 的 ±0.15
NFP_WEAK = 50.0    # 新增就業 <50k 視為疲弱（同 fetch_nfp）
NFP_STRONG = 150.0 # 新增就業 >150k 視為強勁（同 fetch_nfp）


def _fred(series_id: str) -> pd.Series:
    """抓 FRED 月頻序列 → DatetimeIndex(月初) 的 float Series。失敗拋例外。"""
    url = _FRED_CSV.format(sid=series_id)
    r = requests.get(url, timeout=30, verify=False)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), na_values=["."])
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).set_index(date_col)
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna().sort_index()
    s.name = series_id
    return s


def _asof_table(lag_days, values_by_month):
    """
    values_by_month: dict[Timestamp(月初) → 任意值]
    回傳一個函式 f(D) → 在 available_date(=月初+lag) ≤ D 下最近一筆的值（無前視）；無則 None。
    """
    avail = sorted((m + pd.Timedelta(days=lag_days), v) for m, v in values_by_month.items())
    a_dates = [a for a, _ in avail]
    a_vals = [v for _, v in avail]

    def f(D):
        D = pd.Timestamp(D).normalize()
        pos = np.searchsorted(a_dates, D, side="right") - 1
        if pos < 0:
            return None
        return a_vals[pos]
    return f


def build_macro_flags():
    """
    回傳 (dovish_at, hawkish_at)：兩個 f(D) → int 子分（point-in-time，無前視）。
      dovish_score  = (通膨降溫?4:0)+(就業轉弱?3:0) cap 7
      hawkish_score = (通膨升溫?4:0)+(就業強勁?3:0) cap 7
    """
    cpi = _fred("CPIAUCSL")
    pce = _fred("PCEPI")
    nfp = _fred("PAYEMS")
    unr = _fred("UNRATE")

    # YoY → MoM 變化方向（升溫 / 降溫 flag）
    def cool_hot(series):
        yoy = series.pct_change(12) * 100
        d_yoy = yoy.diff()  # 本月 YoY − 上月 YoY
        cool = {m: (v < -TREND_EPS) for m, v in d_yoy.items() if not np.isnan(v)}
        hot = {m: (v > TREND_EPS) for m, v in d_yoy.items() if not np.isnan(v)}
        return cool, hot

    cpi_cool, cpi_hot = cool_hot(cpi)
    pce_cool, pce_hot = cool_hot(pce)

    # 就業：新增就業（PAYEMS MoM 差，千人）+ 失業率變化
    nfp_chg = nfp.diff()
    unr_chg = unr.diff()
    jobs_weak, jobs_strong = {}, {}
    for m in nfp_chg.index:
        ck = nfp_chg.get(m)
        uc = unr_chg.get(m, np.nan)
        if ck is None or np.isnan(ck):
            continue
        # 疲弱：新增就業 <50k 或 失業率上升；強勁：新增就業 >150k 且 失業率未上升
        jobs_weak[m] = bool(ck < NFP_WEAK or (not np.isnan(uc) and uc > 0.05))
        jobs_strong[m] = bool(ck > NFP_STRONG and (np.isnan(uc) or uc <= 0.05))

    f_cpi_cool = _asof_table(_PUB_LAG_DAYS["CPIAUCSL"], cpi_cool)
    f_cpi_hot = _asof_table(_PUB_LAG_DAYS["CPIAUCSL"], cpi_hot)
    f_pce_cool = _asof_table(_PUB_LAG_DAYS["PCEPI"], pce_cool)
    f_pce_hot = _asof_table(_PUB_LAG_DAYS["PCEPI"], pce_hot)
    f_jobs_weak = _asof_table(_PUB_LAG_DAYS["PAYEMS"], jobs_weak)
    f_jobs_strong = _asof_table(_PUB_LAG_DAYS["PAYEMS"], jobs_strong)

    def dovish_at(D):
        h = 0
        if f_cpi_cool(D) or f_pce_cool(D):
            h += 4
        if f_jobs_weak(D):
            h += 3
        return min(h, 7)

    def hawkish_at(D):
        h = 0
        if f_cpi_hot(D) or f_pce_hot(D):
            h += 4
        if f_jobs_strong(D):
            h += 3
        return min(h, 7)

    span = f"{cpi.index.min().date()} ~ {cpi.index.max().date()}"
    print(f"FRED 月頻載入：CPI/PCE/PAYEMS/UNRATE，CPI 區間 {span}")
    return dovish_at, hawkish_at


def _mark_turns(btc, side):
    """side='low' → swing low + 反彈≥18%；side='high' → swing high + 回撤≥18%。回傳 index list。"""
    n = len(btc)
    close = btc["close"].values.astype(float)
    px = (btc["low"] if side == "low" else btc["high"]).values.astype(float)
    out = []
    for i in range(ORDER, n - ORDER):
        w = px[i - ORDER:i + ORDER + 1]
        is_turn = (px[i] == np.nanmin(w)) if side == "low" else (px[i] == np.nanmax(w))
        if not (is_turn and (w == px[i]).sum() == 1):
            continue
        fut = close[i + 1:i + LOOKAHEAD_LABEL + 1]
        if not len(fut):
            continue
        if side == "low" and (fut.max() / close[i] - 1) >= RALLY_THRESH:
            out.append(i)
        elif side == "high" and (1 - fut.min() / close[i]) >= RALLY_THRESH:
            out.append(i)
    return out


def _negatives(btc, turns, era_start, rng):
    n = len(btc)
    era_idx = [k for k in range(n) if btc.index[k] >= era_start and ORDER <= k < n - ORDER]
    pool = [k for k in era_idx if all(abs(k - t) > 45 for t in turns)]
    if not pool:
        return []
    size = min(len(turns) * 2, len(pool))
    return list(rng.choice(pool, size=size, replace=False))


def _dim_auc(btc, pos_idx, neg_idx, score_at, label):
    pos = [score_at(btc.index[k]) for k in pos_idx]
    neg = [score_at(btc.index[k]) for k in neg_idx]
    a = auc(pos, neg)
    pm, nm = np.mean(pos), np.mean(neg)
    # 正樣本中「flag 有觸發(>0)」的比例 — 看訊號是否夠頻繁
    pos_fire = np.mean([s > 0 for s in pos]) * 100
    neg_fire = np.mean([s > 0 for s in neg]) * 100
    print(f"  {label:24s} 轉折均 {pm:4.2f} / 非轉折均 {nm:4.2f}  AUC={a:.3f}  "
          f"觸發率 轉折 {pos_fire:3.0f}% / 非轉折 {neg_fire:3.0f}%")
    return a


def main():
    print("載入 BTC 日線 …")
    btc, _ = fetch_market_data()
    btc = calculate_technical_indicators(btc)
    btc = calculate_ahr999(btc)
    btc = calculate_bear_bottom_indicators(btc)
    if btc.index.tz is not None:
        btc.index = btc.index.tz_localize(None)

    print("載入 FRED macro flags …")
    dovish_at, hawkish_at = build_macro_flags()

    rng = np.random.default_rng(42)
    full_start = btc.index.min()
    fund_start = pd.Timestamp("2021-01-01")

    print("\n" + "=" * 78)
    print("抄底側 · dovish flags（通膨降溫 + 就業轉弱）對『相對底部』判別力")
    print("=" * 78)
    for era_name, era_start in (("全期", full_start), ("資金費率時代(2021+)", fund_start)):
        bottoms = [i for i in _mark_turns(btc, "low") if btc.index[i] >= era_start]
        negs = _negatives(btc, bottoms, era_start, rng)
        if len(bottoms) < 5 or len(negs) < 5:
            print(f"[{era_name}] 樣本不足（底 {len(bottoms)} / 非底 {len(negs)}）"); continue
        print(f"\n[{era_name}] 相對底部 {len(bottoms)} / 負樣本 {len(negs)}")
        _dim_auc(btc, bottoms, negs, dovish_at, "dovish_score(0-7)")

    print("\n" + "=" * 78)
    print("逃頂側 · hawkish flags（通膨升溫 + 就業強勁）對『相對頂部』判別力")
    print("=" * 78)
    for era_name, era_start in (("全期", full_start), ("資金費率時代(2021+)", fund_start)):
        tops = [i for i in _mark_turns(btc, "high") if btc.index[i] >= era_start]
        negs = _negatives(btc, tops, era_start, rng)
        if len(tops) < 5 or len(negs) < 5:
            print(f"[{era_name}] 樣本不足（頂 {len(tops)} / 非頂 {len(negs)}）"); continue
        print(f"\n[{era_name}] 相對頂部 {len(tops)} / 負樣本 {len(negs)}")
        _dim_auc(btc, tops, negs, hawkish_at, "hawkish_score(0-7)")

    # ── 增量檢核（抄底）：把 dovish 疊到既有可擬合複合分，看 test AUC 變化 ──────────
    print("\n" + "=" * 78)
    print("增量檢核（抄底）：dovish 疊加到既有可擬合複合分（funding/tech/fng/cycle）")
    print("=" * 78)
    incremental_check(btc, dovish_at, rng)


def incremental_check(btc, dovish_at, rng):
    """以既有可擬合四維專家複合分為基準，比較『加 dovish vs 不加』的 test AUC（資金費率時代）。"""
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
        print("  F&G 抓取失敗：", e)

    fund_start = pd.Timestamp("2021-01-01")
    bottoms = [i for i in _mark_turns(btc, "low") if btc.index[i] >= fund_start]
    negs = _negatives(btc, bottoms, fund_start, rng)
    if len(bottoms) < 6 or len(negs) < 6:
        print("  樣本不足，跳過增量檢核。"); return

    # 既有四維專家相對權重（funding 25 / tech 20 / fng 15 / cycle 20）正規化 → 0..1
    exp_w = np.array([25, 20, 15, 20]) / 80.0
    maxes = np.array([20.0, 25.0, 10.0, 20.0])

    def base_composite(k):
        ann = annualize_funding(float(fund_daily.get(btc.index[k].normalize(), np.nan))) \
            if not fund_daily.empty else None
        fs = funding_low_score(ann)
        tech = tech_low_score(btc.iloc[k], btc.iloc[:k + 1])
        gs = fng_low_score(fng_map.get(btc.index[k].strftime("%Y-%m-%d"), np.nan))
        cyc = cycle_low_score(btc.iloc[k])
        return float((np.array([fs, tech, gs, cyc]) / maxes) @ exp_w)

    def split(idx):
        order = np.argsort([btc.index[k] for k in idx])
        h = len(order) // 2
        return [idx[i] for i in order[:h]], [idx[i] for i in order[h:]]

    tr_p, te_p = split(bottoms)
    tr_n, te_n = split(negs)

    # macro 子分占整體 max 100 的 7 分 → 疊加時對齊：composite + (dovish/7)*0.07 尺度近似
    # 簡化：以「複合分(0..1) + λ·dovish/7」掃 λ，看 test AUC 是否提升（λ 對齊 macro 權重 0.07）
    def comp(idx, lam):
        return [base_composite(k) + lam * (dovish_at(btc.index[k]) / 7.0) for k in idx]

    print(f"  資金費率時代：train 底 {len(tr_p)}/非底 {len(tr_n)}；test 底 {len(te_p)}/非底 {len(te_n)}")
    base_te = auc(comp(te_p, 0.0), comp(te_n, 0.0))
    print(f"  基準（不加 dovish）           test AUC={base_te:.3f}")
    for lam in (0.05, 0.07, 0.10, 0.15):
        te = auc(comp(te_p, lam), comp(te_n, lam))
        print(f"  +dovish (λ={lam:.2f}, ≈macro權重)  test AUC={te:.3f}  Δ={te - base_te:+.3f}")
    print("  （Δ≥0＝無害/有益；明顯>0 才支持給 dovish 實證權重）")


if __name__ == "__main__":
    main()
