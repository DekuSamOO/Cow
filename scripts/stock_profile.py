"""
scripts/stock_profile.py  ·  個股評價資料收集器（台股／美股通用）

供 `stock-evaluator` agent 使用：**決定性的資料收集放這裡、判讀留給 agent**。
理由是同一支股票在同一天跑兩次應該得到同一組數字——若讓 agent 每次自己拼 SQL 與
指標，數字會隨對話漂移，事後也無從追。本檔輸出 JSON（`--json`）或人類可讀報表。

用法：
    python scripts/stock_profile.py 2330
    python scripts/stock_profile.py AAPL --json

── 資料源分工（台股與美股深度天生不對等，不硬湊）────────────────────────────
  價格/現價/52週   ：Yahoo v8（`service.ohlc_universal`，兩市場同一條路徑，含當日）
  台股 技術/型態   ：tw_stock_climber DB 的 **Adj_Close**（climber 自身慣例，T-10 世代
                     起 detect_* 一律 Adj_Close；用未還原收盤會在除息前後產生假型態）
  台股 估值/籌碼   ：同 DB 的 PE/PB/Yield/三大法人/融資券/TDCC（2016 起全史）
  美股 技術/型態   ：Yahoo OHLCV（**無本地源**，故無估值/籌碼維度——這是事實不是疏漏）
  型態偵測         ：climber `analyzers.panel_indicators` 的純函式（兩市場共用同一份
                     判定邏輯，只是餵不同來源的 panel）

── 誠實邊界（agent 撰寫筆記時必須照抄）──────────────────────────────────────
  1. 「短線特性」欄位（流動性/波動/量能）是**描述性事實**，不是預測；門檻（如日均成交額
     一億）是市場慣例分級，**未經回測**。
  2. 已回測的只有 Cow 台股雷達各維（AUC 見 `_governance/FINDINGS-cow-radar-backtests.md`）。
     **美股沒有**——2026-07-02 回測 50 檔三維全近雜訊 AUC~0.5，Cow 已撤下該面板。
  3. 本檔**不輸出任何綜合「可炒性總分」**。把未回測維度加權成一個分數會讓人以為它經過
     驗證（CONSTITUTION 8-12 量化紀律）。要排序請用各欄位自己看。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _COW not in sys.path:
    sys.path.insert(0, _COW)

# climber：同層 repo（比照 scripts/tw_variant_extract.py 的解析方式），可用環境變數覆蓋
_CLIMBER = os.getenv("TW_CLIMBER_REPO") or os.path.normpath(
    os.path.join(_COW, "..", "tw_stock_climber"))
_CLIMBER_DB = os.getenv("TW_CLIMBER_DB") or os.path.join(
    _CLIMBER, "db", "twse_official_data.db")

from core.indicators import calculate_technical_indicators          # noqa: E402
from core.momentum import momentum_ref_rows                         # noqa: E402
from core.relative_high_tw import (compute_relative_high_tw,        # noqa: E402
                                   relative_high_tw_meta, vol_snapshot)
from core.relative_low_tw import (compute_relative_low_tw,          # noqa: E402
                                  relative_low_tw_meta)
from core.trend_direction import compute_trend_score, trend_meta    # noqa: E402
from service.ohlc_universal import (classify_symbol, fetch_ohlc,    # noqa: E402
                                    fetch_live_quote, _session, _tw_candidates, _YF_CHART)

# 流動性分級門檻（**市場慣例，非回測值**）：日均成交金額，台股新台幣／美股美元。
# 用途只是「這檔進得去出得來嗎」的量級感，不是選股訊號。
_LIQ_TIERS_TW = ((1e8, "充裕"), (1e7, "中等"), (0, "偏薄"))
_LIQ_TIERS_US = ((5e7, "充裕"), (5e6, "中等"), (0, "偏薄"))


# ──────────────────────────────────────────────────────────────────────────
# 資料源
# ──────────────────────────────────────────────────────────────────────────

def _yahoo_meta(yahoo_symbol: str) -> dict:
    """Yahoo v8 chart 的 meta 區（名稱/交易所/幣別/52週高低）。失敗回 {}。"""
    for sym in _tw_candidates(yahoo_symbol):
        try:
            r = _session().get(_YF_CHART + sym, params={"range": "1d", "interval": "1d"},
                               timeout=15)
            r.raise_for_status()
            return r.json()["chart"]["result"][0]["meta"] or {}
        except Exception:      # noqa: BLE001 — 名稱屬加值資訊，取不到不該中斷整份 profile
            continue
    return {}


def _climber_conn():
    """climber DB 連線；找不到檔案回 None（美股本來就用不到，台股則降級並在輸出標明）。"""
    if not os.path.exists(_CLIMBER_DB):
        return None
    return sqlite3.connect(f"file:{_CLIMBER_DB}?mode=ro", uri=True)


def _climber_history(con, stock_id: str, days: int = 3000) -> pd.DataFrame:
    """climber daily_quotes → 近 days 根，欄名轉 Cow 慣例的 lowercase。

    **close 用 Adj_Close**（climber T-10 世代慣例；未還原收盤會在除息前後造出假型態），
    且 **open/high/low 同乘 `Adj_Close/Close` 一起還原**——只還原 close 會讓
    `|high − close.shift()|` 這類跨欄比較吃到還原因子本身：2330 近 400 根的因子從
    0.9744 漂到 1.0000，等於憑空生出最多 2.6% 的假跳空與假 true range。
    climber 自己的 `build_price_panels` 只換 close 欄，本檔刻意不照抄那個混用。

    ⚠️ `days` 預設 3000（≈12 年，涵蓋 climber DB 自 2016 起的全史）**不是隨手取的數**：
    量能/成交額分位的母體就是這段歷史，餵 400 根等於拿 1.7 年的短母體去比，與 Cow 台股
    量能維的校準口徑（expanding，面板自 2016-01-01）不一致——那正是 2026-08-11 修掉的
    同一個病（見 README v3.36）。改小此值＝改分位定義。"""
    q = ("SELECT Date, Open, High, Low, Close, Adj_Close, Volume, PE, PB, Yield, "
         "Foreign_BuySell, Trust_BuySell, Dealer_BuySell, Total_Inst_BuySell, "
         "Margin_Balance, Short_Balance FROM daily_quotes "
         "WHERE Stock_ID = ? ORDER BY Date DESC LIMIT ?")
    df = pd.read_sql_query(q, con, params=[stock_id, days])
    if df.empty:
        return df
    df = df.iloc[::-1].reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    adj = df["Adj_Close"].fillna(df["Close"])
    ratio = (adj / df["Close"]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    df["close"] = adj
    for src, dst in (("Open", "open"), ("High", "high"), ("Low", "low")):
        df[dst] = df[src] * ratio
    return df.rename(columns={"Volume": "volume"})


def _climber_name(con, stock_id: str):
    row = con.execute("SELECT Name, Market FROM stock_list WHERE Stock_ID = ?",
                      [stock_id]).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _climber_tdcc(con, stock_id: str):
    row = con.execute("SELECT Date, major_pct, mid_pct, retail_pct FROM tdcc_holding "
                      "WHERE Stock_ID = ? ORDER BY Date DESC LIMIT 1", [stock_id]).fetchone()
    if not row:
        return None
    return {"as_of": row[0], "major_pct": row[1], "mid_pct": row[2], "retail_pct": row[3]}


# ──────────────────────────────────────────────────────────────────────────
# 指標
# ──────────────────────────────────────────────────────────────────────────

def _tier(value, tiers):
    for lo, label in tiers:
        if value >= lo:
            return label
    return tiers[-1][1]


def short_term_traits(df: pd.DataFrame, is_tw: bool, shares_out=None) -> dict:
    """短線交易特性（**全部是描述性事實，非預測、未回測**）。

    收錄的每一項都必須能回答「這檔股票適不適合短進短出」這個機械問題：
      流動性  → 進得去出得來嗎（日均成交金額；門檻為市場慣例分級）
      波動    → 有沒有值得做的振幅（ATR%、日振幅分位）
      跳空    → 隔日開盤風險多大（停損會不會被跳過去）
      量能    → 現在是活的還是死的（分位＋量比，複用 Cow 量能維同一份實作）
      漲跌停  → 台股 ±10% 制度下的極端日頻率（美股無此制度，欄位為 None）
    """
    out = {}
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    turnover = (close * vol).dropna()
    for w in (20, 60):
        out[f"turnover_{w}d"] = float(turnover.tail(w).mean()) if len(turnover) >= w else None
    tiers = _LIQ_TIERS_TW if is_tw else _LIQ_TIERS_US
    out["liquidity_tier"] = (_tier(out["turnover_20d"], tiers)
                             if out["turnover_20d"] is not None else None)
    out["turnover_unit"] = "TWD" if is_tw else "USD"

    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]
    out["atr14_pct"] = float(atr14 / close.iloc[-1] * 100) if pd.notna(atr14) else None

    amp = ((high - low) / close).tail(60).dropna() * 100
    out["amp_median_pct"] = float(amp.median()) if len(amp) else None
    out["amp_p90_pct"] = float(amp.quantile(0.9)) if len(amp) else None

    gap = (df["open"] / close.shift() - 1).abs().tail(60).dropna() * 100
    out["gap_over_2pct_ratio"] = float((gap > 2).mean()) if len(gap) else None

    snap = vol_snapshot(df)
    out["vol_pctile"] = round(snap["pctile"], 4) if snap else None
    out["vol_ratio_5_60"] = round(snap["ratio"], 3) if snap else None
    # 成交「金額」分位——跨年代比較必須用金額不是股數。Yahoo/climber 的 volume 都已還原
    # 股票分割，但**股數本身會隨股價長期漂移**：NVDA 分年日量中位數 2016 3.97 億股 →
    # 2026 1.59 億股（同期收盤 1.7 → 195.7），股數分位因此顯示「十年最低 0 分位」，
    # 讀起來像「沒人在交易」，實際上日成交額 $26B 是全市場最活躍的一檔。
    # 股數分位保留是因為它是 Cow 台股量能維的**校準口徑**（AUC 0.648，不可換單位），
    # 金額分位則是給人看「這檔現在活不活」的正確問法。兩者並列。
    to5 = turnover.rolling(5).mean().dropna()
    if len(to5) >= 60:
        latest = float(to5.iloc[-1])
        arr = to5.to_numpy(dtype=float)
        out["turnover_pctile"] = round(
            float(((arr < latest).sum() + 0.5 * (arr == latest).sum()) / len(arr)), 4)
    else:
        out["turnover_pctile"] = None

    ret = (close / close.shift() - 1).tail(60).dropna() * 100
    out["limit_move_days_60"] = int((ret.abs() >= 9.5).sum()) if is_tw and len(ret) else None
    out["turnover_rate_pct"] = (float(vol.tail(20).mean() / shares_out * 100)
                                if shares_out else None)
    return out


def climber_patterns(df: pd.DataFrame, stock_id: str) -> dict:
    """climber `analyzers.panel_indicators` 的型態判定（策略二/三/四 = 最貼近「炒短線」）。
    只吃 panel（close/high/low/volume DataFrame），不需要 climber 的 DatabaseManager，
    故台股（climber DB）與美股（Yahoo）能共用**同一份判定邏輯**。
    climber repo 不在或 import 失敗 → 回 {"available": False, ...}，不中斷。"""
    if _CLIMBER not in sys.path:
        sys.path.insert(0, _CLIMBER)
    # climber import 時會 logging.info 印一行 [Settings] DB=… 到 stdout，污染 --json 輸出
    # （JSON 前多一行非 JSON 文字，呼叫端 json.loads 直接炸）。壓到 WARNING 以上。
    import logging
    logging.getLogger("TWStockClimber").setLevel(logging.WARNING)
    try:
        from analyzers.panel_indicators import compute_panel_patterns, compute_panel_features
    except Exception as e:      # noqa: BLE001
        return {"available": False, "reason": f"climber analyzers 不可用：{str(e)[:80]}"}
    sub = df.tail(252)
    panels = {k: pd.DataFrame({stock_id: sub[k].to_numpy(dtype=float)})
              for k in ("close", "high", "low", "volume")}
    panels["stock_ids"] = [stock_id]
    try:
        pat = compute_panel_patterns(panels).get(stock_id, {})
        feat = compute_panel_features(panels).get(stock_id, {})
    except Exception as e:      # noqa: BLE001
        return {"available": False, "reason": f"型態計算失敗：{str(e)[:80]}"}
    return {"available": True, "patterns": {k: bool(v) for k, v in pat.items()},
            "features": {k: (None if v is None or (isinstance(v, float) and np.isnan(v))
                             else float(v)) for k, v in feat.items()}}


# ──────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────

def profile(raw_symbol: str) -> dict:
    info = classify_symbol(raw_symbol)
    is_tw = info["kind"] == "tw_stock"
    if info["kind"] == "crypto":
        raise SystemExit("本工具只評價股票；幣對請用 watcher.py / BTC_WATCH.py")

    yq = fetch_ohlc(info["yahoo"])                 # 10y 日線（Cow 單一價格路徑）
    meta = _yahoo_meta(info["yahoo"])
    live = fetch_live_quote(info["yahoo"])
    con = _climber_conn() if is_tw else None

    # 技術/型態用的 df：台股走 climber Adj_Close（除息還原），美股走 Yahoo
    src_note, tech_df = "Yahoo v8（未還原收盤）", yq
    if is_tw and con is not None:
        cdf = _climber_history(con, info["display"])
        if not cdf.empty and len(cdf) >= 120:
            src_note, tech_df = f"climber DB Adj_Close（至 {cdf.index[-1].date()}）", cdf
    ind = calculate_technical_indicators(tech_df)
    row = ind.iloc[-1]

    price = live.get("price") or float(yq["close"].iloc[-1])
    win = yq.tail(252)
    hi52, lo52 = float(win["high"].max()), float(win["low"].min())

    shares_out = None
    if is_tw:
        try:
            from service.tw_chip import get_shares_outstanding
            shares_out = get_shares_outstanding(info["display"])
        except Exception:      # noqa: BLE001 — 週轉率屬加值欄位
            shares_out = None

    out = {
        "symbol": info["display"],
        "yahoo_symbol": info["yahoo"],
        "market": "台股" if is_tw else "美股",
        "name": meta.get("longName") or meta.get("shortName"),
        "exchange": meta.get("fullExchangeName"),
        "currency": meta.get("currency"),
        # ⚠️ `as_of` 是**執行時鐘**，不是資料時間。盤中跑，現價/漲跌%/52週位置/市值都會漂
        # （實測同日 09:25 與 09:29 兩次：2,400.00 → 2,405.00）。真正的資料截止日看
        # `data_as_of`（技術面/型態/雷達）與 `chip.as_of`／`chip.tdcc.as_of`（籌碼）。
        # 消費端標時間戳必須三個都標，只標 as_of 會產生「假新鮮」的時間戳。
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),   # 舊名保留，語意同 run_at
        "price": price,
        "prev_close": live.get("prev_close"),
        "chg_pct": ((price / live["prev_close"] - 1) * 100
                    if live.get("prev_close") else None),
        "hi_52w": hi52, "lo_52w": lo52,
        "pos_52w_pct": (price - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else None,
        "history_from": str(yq.index[0].date()),
        "bars": len(yq),
        # 分位母體＝tech_df（台股是 climber、美股是 Yahoo），與 history_from 可能不同來源、
        # 不同長度。2026-08-12 驗收抓到 render 拿 history_from 去標分位母體 → 台股顯示
        # 「2016 起全史」但實際只有 400 根 ≈1.7 年。**標示母體一律用這兩個欄位。**
        "tech_from": str(tech_df.index[0].date()),
        "tech_bars": len(tech_df),
        # 涵蓋率＝實際根數 ÷ 該區間應有的交易日數。「X 起 N 根」字面為真卻會**暗示連續性**：
        # climber 的 6782 首列是 2021-01-13、共 893 根，看起來像 5.6 年連續資料，實際
        # 2021 年只有 1 列、2022 年 25 列，2023 才開始完整 → 分位母體其實是 3.6 年。
        # 低涵蓋率不影響分位算得對不對，但會讓「母體＝2021 起」這個標示騙人。
        "tech_coverage": _coverage(tech_df, is_tw),
        "data_as_of": str(tech_df.index[-1].date()),   # 技術面/型態/雷達的資料截止日
        "market_cap": price * shares_out if shares_out else None,
        "shares_outstanding": shares_out,
        "tech_source": src_note,
        "short_term": short_term_traits(tech_df, is_tw, shares_out),
        "patterns": climber_patterns(tech_df, info["display"]),
        "momentum_rows": momentum_ref_rows(ind),
    }

    score, sig = compute_trend_score(row, ind), None
    out["trend"] = {"score": score[0], "label": trend_meta(score[0])[0],
                    "detail": [f"{v['score']:+d}/±{v['max']} {v['label']}"
                               for v in score[1].values()] if isinstance(score[1], dict) else []}

    if is_tw and con is not None:
        cdf_last = tech_df.iloc[-1]
        chip = {
            "valuation": {"pe": _f(cdf_last.get("PE")), "pb": _f(cdf_last.get("PB")),
                          "yield": _f(cdf_last.get("Yield"))},
            "institutional": {"total_net": _f(cdf_last.get("Total_Inst_BuySell")),
                              "foreign": _f(cdf_last.get("Foreign_BuySell")),
                              "trust": _f(cdf_last.get("Trust_BuySell"))},
            "margin": {"fin_chg_pct": _margin_chg(tech_df)},
            "tdcc": _climber_tdcc(con, info["display"]),
            "as_of": str(tech_df.index[-1].date()),
        }
        out["chip"] = chip
        name, market = _climber_name(con, info["display"])
        # 台股**中文名優先**：Yahoo 對台股回的是英文登記名（6782 → "Visco Vision Inc."），
        # 消費端要用中文名寫報告標題時，若只拿得到英文名就得憑記憶翻譯——而憑記憶陳述
        # 正是規範禁止的。climber `stock_list` 有全名，有就蓋掉 Yahoo 的。
        out["name_en"] = out["name"]
        out["name"] = name or out["name"]
        out["tw_market"] = market
        h = compute_relative_high_tw(row, ind, chip=chip)
        lo = compute_relative_low_tw(row, ind, chip=chip)
        out["radar"] = {
            "high": {"score": h[0], "label": relative_high_tw_meta(h[0])[0],
                     "dims": {k: f"{v['score']}/{v['max']} {v['label']}"
                              for k, v in h[1].items()}},
            "low": {"score": lo[0], "label": relative_low_tw_meta(lo[0])[0],
                    "dims": {k: f"{v['score']}/{v['max']} {v['label']}"
                             for k, v in lo[1].items()}},
        }
    else:
        out["chip"] = None
        out["radar"] = None
        out["radar_note"] = ("美股無免費籌碼/估值源；純 OHLCV 三維雷達 2026-07-02 回測 "
                             "50 檔 AUC~0.5 近雜訊，Cow 已撤下該面板 → 本報告不提供美股雷達分數")
    if con is not None:
        con.close()
    return out


def _coverage(df, is_tw: bool):
    """實際根數 ÷ 該起訖區間應有的交易日數（台股 243 根/年、美股 251、實測 Yahoo 10y）。
    <1 代表期間有整段缺漏，母體不如首列日期看起來那麼長。區間過短回 None（分母不穩）。"""
    span_days = (df.index[-1] - df.index[0]).days
    if span_days < 120:
        return None
    expected = span_days / 365.25 * (243 if is_tw else 251)
    return round(min(len(df) / expected, 1.0), 3) if expected > 0 else None


def _f(v):
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _margin_chg(df):
    """融資餘額近 5 日變化%（climber DB 才有 Margin_Balance）。"""
    if "Margin_Balance" not in df.columns:
        return None
    m = pd.to_numeric(df["Margin_Balance"], errors="coerce").dropna()
    if len(m) < 6 or m.iloc[-6] == 0:
        return None
    return float((m.iloc[-1] / m.iloc[-6] - 1) * 100)


# ──────────────────────────────────────────────────────────────────────────
# 輸出
# ──────────────────────────────────────────────────────────────────────────

def _fmt_money(v, unit):
    """金額中文化。台股要有「兆」這一階——2330 市值 6.2e13，只到「億」會印成
    621,080 億元，量級瞬間讀不出來。"""
    if v is None:
        return "—"
    if unit == "TWD":
        if v >= 1e12:
            return f"{v/1e12:.2f} 兆元"
        return f"{v/1e8:.2f} 億元" if v >= 1e8 else f"{v/1e4:,.0f} 萬元"
    if v >= 1e9:
        return f"${v/1e9:,.2f}B"
    return f"${v/1e6:,.1f}M"


def render(p: dict) -> str:
    st, L = p["short_term"], []
    L.append(f"# {p['symbol']} {p['name'] or ''}（{p['market']}）")
    L.append(f"執行時間 {p['run_at']}｜{p.get('exchange') or ''} {p.get('currency') or ''}")
    tdcc_as_of = ((p.get("chip") or {}).get("tdcc") or {}).get("as_of")
    L.append(f"**資料截止**：技術面/型態/雷達 {p['data_as_of']}"
             + (f"｜籌碼估值 {p['chip']['as_of']}" if p.get("chip") else "")
             + (f"｜TDCC {tdcc_as_of}" if tdcc_as_of else "")
             + "｜現價為即時（盤中每次執行都會變）")
    L.append("")
    L.append("## 基本資訊")
    chg = f"{p['chg_pct']:+.2f}%" if p["chg_pct"] is not None else "—"
    L.append(f"- 現價 {p['price']:,.2f}（{chg}）")
    L.append(f"- 52週高/低 {p['hi_52w']:,.2f} / {p['lo_52w']:,.2f}"
             + (f"（位置 {p['pos_52w_pct']:.0f}%）" if p["pos_52w_pct"] is not None else ""))
    if p["market_cap"]:
        L.append(f"- 市值 {_fmt_money(p['market_cap'], st['turnover_unit'])}"
                 f"（已發行 {p['shares_outstanding']:,.0f} 股）")
    L.append(f"- 資料涵蓋 {p['history_from']} 起 {p['bars']:,} 根日線｜技術面來源：{p['tech_source']}")
    L.append("")
    L.append("## 短線交易特性〔描述性事實，非預測；門檻為市場慣例分級，未回測〕")
    L.append(f"- 流動性　20日均成交額 {_fmt_money(st['turnover_20d'], st['turnover_unit'])}"
             f"｜60日 {_fmt_money(st['turnover_60d'], st['turnover_unit'])}"
             f"　→ **{st['liquidity_tier']}**")
    if st["turnover_rate_pct"] is not None:
        L.append(f"- 週轉率　20日均量 ÷ 已發行股數 = {st['turnover_rate_pct']:.2f}%/日")
    L.append(f"- 波動度　ATR(14) {st['atr14_pct']:.2f}%/日"
             f"｜近60日**盤中**振幅 中位 {st['amp_median_pct']:.2f}% / 90分位 {st['amp_p90_pct']:.2f}%")
    # 這兩個數字**不可相減當作跳空幅度**：ATR 是 14 日平均、振幅是 60 日中位數，窗口與
    # 統計量都不同，且振幅右偏（中位 << 90 分位）本就讓均值高於中位。
    # 舊版這裡有一句無條件樣板「ATR 明顯較大＝波動主要在開盤那一跳」，不看任何數字、
    # 對跳空僅 5% 的 6782 也照印，等於用固定文字冒充判讀（2026-08-12 驗收抓到）。
    # 要談跳空就直接看下一行的實測比率。
    L.append("　　　　　（兩者窗口/統計量不同，不可相減；隔夜風險看下一行實測跳空比率）")
    L.append(f"- 跳空　　近60日開盤跳空 >2% 佔 {st['gap_over_2pct_ratio']*100:.0f}%"
             "（停損被跳過去的風險）")
    # 母體一律標 tech_from/tech_bars（分位真正算在 tech_df 上），**不可用 history_from**
    # ——台股 tech_df 來自 climber、Yahoo 只提供價格，兩者長度不同（2026-08-12 驗收抓到
    # 台股顯示「2016 起全史」但實際只有 400 根 ≈1.7 年）。
    pop = f"母體＝{p['tech_from']} 起 {p['tech_bars']:,} 根"
    cov = p.get("tech_coverage")
    if cov is not None and cov < 0.9:
        pop += f"，**但涵蓋率僅 {cov*100:.0f}%**（期間有整段缺漏，母體沒有起始日看起來那麼長）"
    if st["turnover_pctile"] is not None:
        L.append(f"- 活躍度　5日均**成交額** {st['turnover_pctile']*100:.0f} 分位"
                 f"（{pop}）← 跨年代比較看這個")
    if st["vol_pctile"] is not None:
        L.append(f"- 量能　　5日均**股數** {st['vol_pctile']*100:.0f} 分位（{pop}）"
                 f"｜5日/60日**股數**量比 {st['vol_ratio_5_60']:.2f}x")
        L.append("　　　　　（股數是 Cow 台股量能維的校準口徑 AUC 0.648，不可換單位；"
                 "但股價長期上漲會讓股數分位失真 → 判讀活躍度看上一行的成交額）")
    if st["limit_move_days_60"] is not None:
        L.append(f"- 極端日　近60日單日 ±9.5% 以上 {st['limit_move_days_60']} 天（台股 ±10% 制度）")
    L.append("")
    L.append(f"## 趨勢（Cow 通用軸）\n- {p['trend']['score']:+d}/±100　{p['trend']['label']}")
    for d in p["trend"]["detail"]:
        L.append(f"  - {d}")
    for r in p["momentum_rows"]:
        L.append(f"- {r.strip()}")
    L.append("")
    pat = p["patterns"]
    L.append("## 型態（climber 策略二/三/四判定邏輯）")
    if not pat.get("available"):
        L.append(f"- ⚠ {pat.get('reason')}")
    else:
        hit = [k for k, v in pat["patterns"].items() if v]
        L.append(f"- 成立：{'、'.join(hit) if hit else '（無）'}")
        L.append(f"- 未成立：{'、'.join(k for k, v in pat['patterns'].items() if not v) or '（無）'}")
    L.append("")
    if p["radar"]:
        L.append("## 已回測雷達（台股籌碼，AUC 見 FINDINGS-cow-radar-backtests）")
        for side, zh in (("high", "逃頂"), ("low", "抄底")):
            r = p["radar"][side]
            L.append(f"- **{zh} {r['score']}/100　{r['label']}**")
            for k, v in r["dims"].items():
                L.append(f"  - {k}：{v}")
        c = p["chip"]
        L.append(f"- 籌碼估值截至 {c['as_of']}：PE {c['valuation']['pe']}｜PB {c['valuation']['pb']}"
                 f"｜殖利率 {c['valuation']['yield']}")
        if c["tdcc"]:
            L.append(f"  - TDCC {c['tdcc']['as_of']}：大戶 {c['tdcc']['major_pct']}%"
                     f"／散戶 {c['tdcc']['retail_pct']}%")
    else:
        L.append(f"## 已回測雷達\n- ⚠ {p['radar_note']}")
    return "\n".join(L)


def main():
    # Windows 中文主控台預設 cp950，印不出 🟢🟡🔴 與 JSON 的非 ASCII → UnicodeEncodeError、
    # exit 1、零輸出。2026-08-12 stock-evaluator 首次驗收時兩支 agent 都被這個擋住：
    # 開發期每次測試都習慣性前綴 PYTHONIOENCODING=utf-8，等於每次都繞過了這個 bug，
    # 要有人在乾淨環境跑才會現形。呼叫端不該需要記得設環境變數，故在此自理。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):   # 已被重導向/包裝過的串流沒有此方法
            pass
    ap = argparse.ArgumentParser(description="個股評價資料收集（台股／美股）")
    ap.add_argument("symbol", help="代號：2330 / 6782 / AAPL / NVDA")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 供程式消費")
    a = ap.parse_args()
    p = profile(a.symbol)
    print(json.dumps(p, ensure_ascii=False, indent=2, default=str) if a.json else render(p))


if __name__ == "__main__":
    main()
