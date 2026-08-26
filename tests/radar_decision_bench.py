"""
tests/radar_decision_bench.py
逃頂／抄底雷達「能不能當判斷依據」端到端評分台（2026-08-26）

手動執行（非 pytest）：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/radar_decision_bench.py
  --asset crypto,us        只跑指定資產類別（預設全跑；tw 見 --asset tw 的前置需求）
  --json out.json          把基線寫成 JSON，供改動後逐格對拍

**方法一律 import tests/radar_eval_standard**（規格正本：vault `Github\\Cow\\雷達評估標準.md`）。
事件門檻是**該標的自身的波動倍數**，不是固定 18%——後者在 BTC 是 0.69σ、在 SPY 是 2.38σ，
三個市場的數字從來就不可互相比較。

**與 tests/radar_subitem_audit.py 的分工**：
  radar_subitem_audit  量「單一子項」的觸發率／AUC → 這個子項有沒有訊號
  本檔                 量「總分」的門檻決策品質   → 照這個分數操作會對幾次、漏幾次、提前幾天
子項全部及格不代表總分可用，反之亦然。2026-08-26 實例：逃頂總分 AUC 0.624 看似及格，
但生產門檻 45 的 recall 只有 14%——它在 86% 的頂完全沒響，**子項 AUC 表看不出這件事**。

回放口徑（誠實列出不可得，不假裝有數字）：
  加密  OI 分位／BTC.D／macro 無長史 → 一律 None（與線上灰燈一致）；其餘 PiT 截斷後全餵
  美股  純 OHLCV（本來就沒有免費籌碼源）→ 無缺項
  台股  需重建 chip bundle 歷史，尚未實作 → 目前跳過並明說
"""
import argparse
import json
import os
import sqlite3
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from tests.radar_eval_standard import (realized_sigma_h, swing_events, base_rate,
                                       auc, print_threshold_table, event_window_mask,
                                       ORDER, H, K_TOP, K_BOT, LEAD, GRACE)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAIN_END = "2024-01-01"
DIV_WINDOW = 140


# ══════════════════════════════════════════════════════════════════════════
# 資料載入
# ══════════════════════════════════════════════════════════════════════════
def load_crypto():
    """BTC 日線 + 全部 PiT 可得的外部維度（沿用 radar_subitem_audit.load_all）。"""
    from tests.radar_subitem_audit import load_all
    return load_all()


def load_us_prices(tickers, rng="10y"):
    from core.indicators import calculate_technical_indicators
    from service.ohlc_universal import fetch_ohlc
    out = {}
    for t in tickers:
        try:
            df = calculate_technical_indicators(fetch_ohlc(t, rng=rng))
            if df is not None and len(df) > 400:
                out[t] = df
        except Exception as e:
            print("  %s 抓取失敗（%s）→ 跳過，不假造" % (t, str(e)[:60]))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 逐日重放
# ══════════════════════════════════════════════════════════════════════════
def replay_crypto(btc, fund, mvrv, sopr, etf, fng, start="2019-09-10"):
    from core.relative_high import compute_escape_top_score
    from core.relative_low import compute_relative_low_score
    from service.etf_flow import _summarize
    idx = btc.index[btc.index >= start]
    fund_dates = set(fund.index)
    fng_items, sopr_items = sorted(fng.items()), sorted(sopr.items())
    rows = []
    for d in idx:
        key = d.strftime("%Y-%m-%d")
        i = btc.index.get_loc(d)
        row = btc.iloc[i]
        sub = btc.iloc[max(0, i - DIV_WINDOW):i + 1]
        f8h = float(fund.loc[d]) / 1095 if d in fund_dates else None
        hist = [float(v) for v in fund[fund.index <= d].tail(900).values] or None
        etf_pit = {k: v for k, v in etf.items() if k <= key}
        etf_sum = _summarize(etf_pit) if etf_pit else None
        mv, sp, fg = mvrv.get(key), sopr.get(key), fng.get(key)
        fng_h = [v for k, v in fng_items if k <= key][-400:] or None
        sopr_h = [v for k, v in sopr_items if k <= key][-400:] or None
        t = compute_escape_top_score(row, sub, funding_8h=f8h, oi_stats=None,
                                     etf_summary=etf_sum, sopr=sp, fng=fg,
                                     btc_d_trend=None, macro=None, mvrv_z=mv,
                                     funding_ann_hist=hist)[0]
        l = compute_relative_low_score(row, sub, funding_8h=f8h, oi_stats=None,
                                       etf_summary=etf_sum, sopr=sp, fng=fg,
                                       btc_d_trend=None, macro=None, mvrv_z=mv,
                                       funding_ann_hist=hist, fng_hist=fng_h,
                                       sopr_hist=sopr_h, rsi_pct_enabled=True)[0]
        rows.append((d, t, l))
    return pd.DataFrame(rows, columns=["date", "top", "low"]).set_index("date")


def replay_us(df):
    """美股三維（純 OHLCV）逐日重放。"""
    from core.relative_high_us import compute_relative_high_us
    from core.relative_low_us import compute_relative_low_us
    rows = []
    for i in range(250, len(df)):
        row = df.iloc[i]
        sub = df.iloc[max(0, i - DIV_WINDOW):i + 1]
        rows.append((df.index[i],
                     compute_relative_high_us(row, sub)[0],
                     compute_relative_low_us(row, sub)[0]))
    return pd.DataFrame(rows, columns=["date", "top", "low"]).set_index("date")


# ══════════════════════════════════════════════════════════════════════════
# 評估
# ══════════════════════════════════════════════════════════════════════════
def evaluate(label, scores: pd.DataFrame, close: pd.Series, thresholds_top,
             thresholds_low, train_end=TRAIN_END, quiet=False):
    """對一個標的的 top/low 兩條分數序列做完整評估 → dict。"""
    close = close.reindex(scores.index)
    sig = realized_sigma_h(close)
    out = {}
    for side, col, is_top, ths in (("逃頂", "top", True, thresholds_top),
                                   ("抄底", "low", False, thresholds_low)):
        s = scores[col].astype(float)
        ev = swing_events(close.values, is_top, sigma_h=sig.values)
        n = len(s)
        win = event_window_mask(ev, n, is_top)
        far = ~event_window_mask(ev, n, is_top) if not len(ev) else np.array(
            [min((abs(i - e) for e in ev), default=999) > LEAD for i in range(n)])
        a_all = auc(s.values[win], s.values[far])
        tr = s.index < train_end
        a_tr = auc(s.values[win & tr], s.values[far & tr])
        a_ho = auc(s.values[win & ~tr], s.values[far & ~tr])
        ev_scores = s.values[[e for e in ev if 0 <= e < n]] if ev else np.array([])
        rec = {"n_days": n, "n_events": len(ev),
               "events_per_year": round(len(ev) / max((s.index[-1] - s.index[0]).days / 365.25, 1e-9), 2),
               "auc_all": None if not np.isfinite(a_all) else round(a_all, 3),
               "auc_train": None if not np.isfinite(a_tr) else round(a_tr, 3),
               "auc_holdout": None if not np.isfinite(a_ho) else round(a_ho, 3),
               "score_p50": float(np.nanpercentile(s.values, 50)),
               "score_p95": float(np.nanpercentile(s.values, 95)),
               "score_max": float(np.nanmax(s.values)),
               "event_score_median": float(np.median(ev_scores)) if len(ev_scores) else None,
               "event_score_max": float(np.max(ev_scores)) if len(ev_scores) else None}
        if not quiet:
            print()
            print("  【%s %s】n=%d 日｜事件 %d 次（%.2f 次/年）"
                  % (label, side, n, len(ev), rec["events_per_year"]))
            print("  AUC 全期 %s｜train %s｜holdout %s"
                  % (rec["auc_all"], rec["auc_train"], rec["auc_holdout"]))
            print("  分數：全期 P50 %.0f／P95 %.0f／max %.0f｜事件當天中位 %s／max %s"
                  % (rec["score_p50"], rec["score_p95"], rec["score_max"],
                     "—" if rec["event_score_median"] is None else "%.0f" % rec["event_score_median"],
                     "—" if rec["event_score_max"] is None else "%.0f" % rec["event_score_max"]))
            rec.update(print_threshold_table("", s.values, ev, is_top, ths))
        else:
            rec["base_rate"] = base_rate(ev, n, is_top)
        out[col] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="crypto,us")
    ap.add_argument("--json", default=None)
    ap.add_argument("--us-tickers", default="SPY,QQQ,AAPL,NVDA,MSFT,AMZN,GOOGL,META")
    args = ap.parse_args()
    assets = [a.strip() for a in args.asset.split(",") if a.strip()]
    out = {"standard": {"ORDER": ORDER, "H": H, "K_TOP": K_TOP, "K_BOT": K_BOT,
                        "LEAD": LEAD, "GRACE": GRACE}}

    print("=" * 96)
    print("雷達決策評分台　·　事件門檻＝波動標準化（k_top=%.2f, k_bot=%.2f, H=%d, ORDER=%d）"
          % (K_TOP, K_BOT, H, ORDER))
    print("規格正本：vault Github\\Cow\\雷達評估標準.md")
    print("=" * 96)

    if "crypto" in assets:
        print()
        print("### 加密（BTCUSDT）")
        btc, fund, mvrv, sopr, etf, fng = load_crypto()
        sc = replay_crypto(btc, fund, mvrv, sopr, etf, fng)
        close = btc["close"] if "close" in btc.columns else btc["Close"]
        out["crypto_BTCUSDT"] = evaluate("BTC", sc, close,
                                         [25, 35, 40, 45, 49, 51], [30, 40, 45, 50, 54, 56])

    if "us" in assets:
        print()
        print("### 美股（三維純 OHLCV；面板 2026-07-04 已撤下，此處是重新驗證）")
        prices = load_us_prices([t.strip() for t in args.us_tickers.split(",")])
        agg = {"top": [], "low": []}
        for t, df in prices.items():
            sc = replay_us(df)
            r = evaluate(t, sc, df["close"], [30, 40, 50, 60], [30, 40, 50, 60], quiet=True)
            out["us_%s" % t] = r
            for k in ("top", "low"):
                agg[k].append((t, r[k]))
        for k, side, is_top in (("top", "逃頂", True), ("low", "抄底", False)):
            print()
            print("  【美股 %s】各標的（quiet 模式，只報關鍵指標）" % side)
            print("  %-8s %-8s %-11s %-11s %-11s %s"
                  % ("標的", "事件數", "AUC全期", "AUC holdout", "隨機基準", "事件當天中位分"))
            for t, r in agg[k]:
                print("  %-8s %-8d %-11s %-11s %-11s %s"
                      % (t, r["n_events"], r["auc_all"], r["auc_holdout"],
                         "%.0f%%" % (r["base_rate"] * 100) if np.isfinite(r["base_rate"]) else "—",
                         "—" if r["event_score_median"] is None else "%.0f" % r["event_score_median"]))
            aucs = [r["auc_all"] for _, r in agg[k] if r["auc_all"] is not None]
            if aucs:
                print("  → 跨標的 AUC 中位 %.3f（0.5＝無訊號）" % float(np.median(aucs)))

    if "tw" in assets:
        print()
        print("### 台股：**尚未實作**")
        print("  需要重建 chip bundle 的歷史序列（PE/PB/融資/法人/TDCC 逐日組裝成")
        print("  service.tw_chip.get_chip_bundle 的結構）才能重放總分。")
        print("  scripts/data/tw_calib_panel.parquet 只有原始維度輸入，沒有 bundle 結構。")
        print("  → 本輪跳過，不給假數字。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=float)
        print()
        print("基線已寫入 %s" % args.json)


if __name__ == "__main__":
    main()
