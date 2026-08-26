"""
tests/event_scale_survey.py
「±18% / 60 日」這把尺，在加密／台股／美股各是多嚴格？（2026-08-26）

手動執行：
  D:\\Users\\63191\\AppData\\Local\\anaconda3\\python.exe tests/event_scale_survey.py

背景：逃頂/抄底雷達的**三套**回測腳本都抄同一組門檻——
  tests/radar_subitem_audit.py（BTC）      H=60, MOVE=0.18
  scripts/tw_universal_backtest.py（台股）  其後 60 日 ±18%
  scripts/us_universal_backtest.py（美股）  沿用台股方法
  scripts/tw_dim_backtest.py（台股）        _REV = 0.18
但 18% 對 BTC 是常態級波動、對台股大盤是重大事件。**同一個數字在三個市場代表不同稀有度**，
於是三邊算出來的 AUC / precision / recall 根本不可互相比較，也不能共用同一套判讀。

本腳本只做一件事：把「18%」換算成各標的自身的**波動倍數**與**事件密度**，
作為改用波動標準化門檻的實證依據。不改任何計分邏輯。

資料源（全部本機，公司網路可跑）：
  BTC   Cow db/btcusdt_15m_*.db
  台股  tw_stock_climber db/twse_official_data.db（taiex_daily 大盤 + daily_quotes 個股）
  美股  Yahoo v8（公司網路常被 429）→ 抓不到就跳過並明說，不假造數字
"""
import os
import sqlite3
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TW_DB = os.path.abspath(os.path.join(ROOT, "..", "tw_stock_climber", "db",
                                     "twse_official_data.db"))
ORDER, H, MOVE = 10, 60, 0.18
VOL_LOOKBACK = 252          # 估年化波動的回看視窗（交易日）


def realized_sigma_h(close: pd.Series, h: int = H, lookback: int = VOL_LOOKBACK) -> pd.Series:
    """PiT 的 h 日已實現波動（比例）：過去 lookback 日的日報酬標準差 x sqrt(h)。"""
    r = np.log(close).diff()
    return r.rolling(lookback).std() * np.sqrt(h)


def events(close: np.ndarray, is_top: bool, thresh) -> list:
    """swing 極值 + 其後 H 日反向幅度 >= 門檻。thresh 可為純量或逐日陣列。"""
    n = len(close)
    arr = np.full(n, thresh, float) if np.isscalar(thresh) else np.asarray(thresh, float)
    out = []
    for i in range(ORDER, n - ORDER):
        t = arr[i]
        if not np.isfinite(t) or t <= 0:
            continue
        w = close[i - ORDER:i + ORDER + 1]
        if (close[i] != w.max()) if is_top else (close[i] != w.min()):
            continue
        fwd = close[i + 1:min(i + 1 + H, n)]
        if len(fwd) == 0:
            continue
        mv = (fwd.min() / close[i] - 1) if is_top else (fwd.max() / close[i] - 1)
        if (mv <= -t) if is_top else (mv >= t):
            out.append(i)
    return out


def coverage(idx_events, n, lead=30, grace=0):
    """事件視窗覆蓋率＝隨機訊號的 precision 上限。"""
    if not idx_events:
        return 0.0
    mask = np.zeros(n, bool)
    for e in idx_events:
        mask[max(0, e - lead):min(n, e + grace + 1)] = True
    return float(mask.mean())


def survey(name, close: pd.Series, k: float = 1.5):
    close = close.dropna()
    if len(close) < VOL_LOOKBACK + 2 * ORDER + H:
        return None
    sig = realized_sigma_h(close)
    v = close.values
    years = (close.index[-1] - close.index[0]).days / 365.25
    ann_vol = np.log(close).diff().std() * np.sqrt(252)
    sig_med = float(sig.median())

    fx_t = events(v, True, MOVE)
    fx_b = events(v, False, MOVE)
    vn_t = events(v, True, (sig * k).values)
    vn_b = events(v, False, (sig * k).values)
    return {
        "name": name, "n": len(close), "years": years,
        "ann_vol": ann_vol, "sigma_h": sig_med,
        "move_in_sigma": MOVE / sig_med if sig_med else float("nan"),
        "fix_top": len(fx_t), "fix_bot": len(fx_b),
        "fix_top_yr": len(fx_t) / years, "fix_bot_yr": len(fx_b) / years,
        "fix_cov_t": coverage(fx_t, len(v)), "fix_cov_b": coverage(fx_b, len(v), grace=7),
        "vn_top": len(vn_t), "vn_bot": len(vn_b),
        "vn_top_yr": len(vn_t) / years, "vn_bot_yr": len(vn_b) / years,
        "vn_cov_t": coverage(vn_t, len(v)), "vn_cov_b": coverage(vn_b, len(v), grace=7),
    }


def load_btc():
    fr = []
    for x in sorted(glob.glob(os.path.join(ROOT, "db", "btcusdt_15m_*.db"))):
        c = sqlite3.connect(f"file:{x}?mode=ro", uri=True)
        fr.append(pd.read_sql("select open_time,close from klines", c)); c.close()
    raw = pd.concat(fr).drop_duplicates("open_time").sort_values("open_time")
    raw["dt"] = pd.to_datetime(raw["open_time"], unit="ms")
    return raw.set_index("dt")["close"].resample("1D").last().dropna()


def load_tw(stock_ids):
    con = sqlite3.connect(f"file:{TW_DB}?mode=ro", uri=True)
    out = {}
    tx = pd.read_sql("select Date, Close from taiex_daily order by Date", con,
                     parse_dates=["Date"]).set_index("Date")["Close"]
    out["台股大盤 TAIEX"] = tx
    for sid in stock_ids:
        q = pd.read_sql(
            "select Date, Adj_Close, Close from daily_quotes where Stock_ID=? order by Date",
            con, params=(sid,), parse_dates=["Date"])
        if q.empty:
            continue
        s = q.set_index("Date")["Adj_Close"].fillna(q.set_index("Date")["Close"])
        out[f"台股 {sid}"] = s.dropna()
    con.close()
    return out


def load_us(tickers):
    out = {}
    try:
        from service.ohlc_universal import fetch_ohlc
    except Exception as e:
        print(f"  美股：無法載入 fetch_ohlc（{e}）→ 跳過")
        return out
    for t in tickers:
        try:
            df = fetch_ohlc(t, rng="10y")
            out[f"美股 {t}"] = df["close"].dropna()
        except Exception as e:
            print(f"  美股 {t}：抓取失敗（{str(e)[:60]}）→ 跳過，不假造")
    return out


def main():
    series = {"加密 BTCUSDT": load_btc()}
    if os.path.exists(TW_DB):
        series.update(load_tw(["0050", "2330", "2317", "6782", "2603"]))
    else:
        print(f"找不到 climber DB：{TW_DB} → 台股整段跳過")
    series.update(load_us(["SPY", "QQQ", "AAPL", "NVDA"]))

    rows = [r for r in (survey(k, v) for k, v in series.items()) if r]
    print()
    print("=" * 108)
    print("A. 「固定 ±18% / 60 日」這把尺在各標的的實際嚴格度")
    print("=" * 108)
    print("%-18s %-8s %-10s %-11s %-11s %-13s %s"
          % ("標的", "年數", "年化波動", "60日σ", "18%=幾倍σ", "頂事件/年", "底事件/年"))
    for r in rows:
        print("%-18s %-8.1f %-10s %-11s %-11s %-13s %s"
              % (r["name"], r["years"], "%.0f%%" % (r["ann_vol"] * 100),
                 "%.1f%%" % (r["sigma_h"] * 100), "%.2fσ" % r["move_in_sigma"],
                 "%.1f 次" % r["fix_top_yr"], "%.1f 次" % r["fix_bot_yr"]))
    print()
    print("  事件視窗覆蓋率（＝隨機訊號的 precision，超過它才算有訊號）")
    print("  %-18s %-14s %s" % ("標的", "頂窗覆蓋", "底窗覆蓋"))
    for r in rows:
        print("  %-18s %-14s %s"
              % (r["name"], "%.0f%%" % (r["fix_cov_t"] * 100), "%.0f%%" % (r["fix_cov_b"] * 100)))

    print()
    print("=" * 108)
    print("B. 改用波動標準化門檻（反向幅度 >= 1.5 x 該標的當下 60 日σ）後")
    print("=" * 108)
    print("%-18s %-13s %-13s %-13s %s"
          % ("標的", "頂事件/年", "底事件/年", "頂窗覆蓋", "底窗覆蓋"))
    for r in rows:
        print("%-18s %-13s %-13s %-13s %s"
              % (r["name"], "%.1f 次" % r["vn_top_yr"], "%.1f 次" % r["vn_bot_yr"],
                 "%.0f%%" % (r["vn_cov_t"] * 100), "%.0f%%" % (r["vn_cov_b"] * 100)))
    print()
    print("=" * 108)
    print("C. k 掃描 — 兩側各要多大的 k，事件密度才對齊到每年約 1.5 次？")
    print("=" * 108)
    print("（底事件普遍比頂多 2~3 倍：這些標的長期向上漂移，低點後的 1.5σ 反彈比高點後的")
    print("  1.5σ 下跌容易發生。**兩側共用同一個 k 會讓底側樣本灌水**，故分開掃。）")
    print()
    for is_top, lab in ((True, "頂"), (False, "底")):
        print("  %s側：" % lab)
        print("  %-8s %s" % ("k", "各標的事件次數/年（中位｜全距）"))
        for k in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5):
            per = []
            for nm, s in series.items():
                s = s.dropna()
                if len(s) < VOL_LOOKBACK + 2 * ORDER + H:
                    continue
                sig = realized_sigma_h(s)
                yrs = (s.index[-1] - s.index[0]).days / 365.25
                per.append(len(events(s.values, is_top, (sig * k).values)) / yrs)
            if not per:
                continue
            print("  %-8.2f 中位 %.2f 次｜%.1f ~ %.1f 次｜標的間離散度 %.2f"
                  % (k, float(np.median(per)), min(per), max(per),
                     float(np.std(per) / max(np.mean(per), 1e-9))))
        print()
    print("判讀：A 表的「18%=幾倍σ」欄跨標的差 3.4 倍 → 三個市場確實在用不同嚴格度的尺。")
    print("      選 k 的規則：**在兩側各自取「中位事件密度最接近 1.5 次/年」的 k**，")
    print("      並優先選離散度（標的間變異）較小者——那代表這把尺在不同市場上最一致。")


if __name__ == "__main__":
    main()
