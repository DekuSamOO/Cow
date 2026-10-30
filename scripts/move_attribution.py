# -*- coding: utf-8 -*-
"""
scripts/move_attribution.py · BTC 異常波動歸因：總經／全市場 vs 個例（2026-10-30 立）

**為什麼要有這支**：2026-10-30 凌晨 BTC −6%，使用者問「總經還是個例？跟幣安被盜有關嗎？」。
待庵當場臨時抓 klines、未平倉量、Coinbase 價差拼出答案，結論是「全市場同步跌、
BNB 在 04:30~04:45 多跌一截」。這支把那套判讀固定下來，下次一個指令跑完。

判讀三問（全部用同一段「事件區間」= BTC 視窗內最大一段跌幅或漲幅的 峰→谷）：
  ① 同步嗎？   各幣起動時間差（onset spread）
  ② 照比例嗎？ 各幣實際報酬 vs「beta × BTC 報酬」（beta 取前 30 天 1h 報酬回歸）
  ③ 有剩嗎？   扣掉 beta 後的殘差，換算成 z 值（殘差 ÷ 歷史殘差波動 × √小時數）
外加四個疊加線索：交易所價差（幣安 vs Coinbase）、未平倉量變化（槓桿清算）、
總經事件／美股開收盤時點、新聞標題（RSS，只有標題、內文未查證）。

⚠ 門檻（同步 30 分、殘差 |z|≥2、價差 0.3%、OI −3%）是描述性判讀門檻，**沒有回測驗證**，
   不可拿來當進出場規則（CONSTITUTION 第 8-12 條）。本檔只報歸因，不下操作結論；
   部位解讀交給 `scripts/sop_status.py` ＋ vault `1a BTC部位SOP.md`。

用法：
  python scripts/move_attribution.py                 # 最近 24 小時
  python scripts/move_attribution.py --hours 12
  python scripts/move_attribution.py --end "2026-10-30 09:00"   # 台北時間
  python scripts/move_attribution.py --json
"""
import argparse
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

TPE = timezone(timedelta(hours=8))
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"]
BAR_MIN = 15
HIST_DAYS = 30

PRE_HOURS = 2              # 視窗前多抓的小時數（量能、價差基準）
SYNC_MAX_MIN = 30         # 起動時間差 ≤ 30 分 → 同步
RESID_Z = 2.0              # |殘差 z| ≥ 2 → 該幣有個別成分
PREMIUM_SHIFT_PCT = 0.3    # 幣安 vs Coinbase 價差偏移 ≥ 0.3% → 交易所特定
OI_DROP_PCT = -3.0         # 事件中 BTC 永續 OI 降 ≥ 3% → 槓桿清算成分
SAME_DIR_HI = 0.8          # ≥ 80% 的幣同向且達 beta 一半 → 全市場
SAME_DIR_LO = 0.5          # < 50% → BTC 個別

NEWS_KEYWORDS = ("hack", "exploit", "drain", "stolen", "breach", "outflow", "withdraw",
                 "sec ", "etf", "fed", "fomc", "rate", "cpi", "pce", "tariff", "liquidat",
                 "binance", "coinbase", "tether", "usdt", "war", "iran", "china")


# ════════════════════════════════════════════════════════════════
# 純計算（可單測，不碰網路）
# ════════════════════════════════════════════════════════════════

def find_event(closes):
    """在一串收盤價中找最大的「峰→谷」跌幅與「谷→峰」漲幅，取絕對值大者。
    回傳 (start_idx, end_idx, pct)；pct 以起點收盤計，start < end。
    只用收盤價：同一根 K 的高低點先後未知，拿高低價找峰谷會把單根插針判成事件，
    且事件幅度會和後面收盤口徑的報酬／殘差對不上（2026-10-30 finance-calc-reviewer 指出）。"""
    # 用對數報酬比大小：簡單 % 會偏向挑「漲」（V 型 100→95→100 的 +5.26% 會蓋過 −5%）
    best, best_log = (0, 0, 0.0), 0.0
    hi_i = lo_i = 0
    for i in range(1, len(closes)):
        dd = math.log(closes[i] / closes[hi_i])
        ru = math.log(closes[i] / closes[lo_i])
        if dd < 0 and -dd > best_log:
            best, best_log = (hi_i, i, (closes[i] / closes[hi_i] - 1) * 100), -dd
        if ru > 0 and ru > best_log:
            best, best_log = (lo_i, i, (closes[i] / closes[lo_i] - 1) * 100), ru
        if closes[i] > closes[hi_i]:
            hi_i = i
        if closes[i] < closes[lo_i]:
            lo_i = i
    return best


def hourly_returns(series):
    """series：{時間戳: 收盤}。回傳 {時間戳: 對前一根的報酬}，只取前後剛好相隔 1 小時的配對（缺口不跨接）。"""
    ts = sorted(series)
    return {b: series[b] / series[a] - 1 for a, b in zip(ts, ts[1:]) if b - a == 3_600_000}


def beta_and_resid_std(y, x):
    """y 對 x 的 OLS 斜率與殘差標準差（皆為小數報酬）。
    y、x 為 {時間戳: 報酬}，只用兩邊都有的時間戳（某幣 K 線有缺口時不錯位）。"""
    common = sorted(set(y) & set(x))
    n = len(common)
    if n < 24:
        return None, None
    y, x = [y[t] for t in common], [x[t] for t in common]
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((a - mx) ** 2 for a in x)
    if vx == 0:
        return None, None
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / vx
    res = [c - my - b * (a - mx) for a, c in zip(x, y)]
    sd = math.sqrt(sum(r * r for r in res) / (n - 2))
    return b, sd


def onset_index(closes, i0, i1, bar_sigma, k=3.0):
    """事件區間內，第一根「順事件方向、單根報酬 ≥ k × 單根歷史波動」的 bar index。
    沒有任何一根達標時，回傳順方向單根報酬最大的那根。
    （不用「累積達全段 30%」：長事件會被前段緩漲緩跌拖偏，2026-10-30 XRP 被判成提早 90 分起動）"""
    total = closes[i1] / closes[i0] - 1
    if total == 0 or i1 <= i0:
        return None
    sign = 1 if total > 0 else -1
    moves = [((closes[i] / closes[i - 1] - 1) * sign, i) for i in range(i0 + 1, i1 + 1)]
    if bar_sigma:
        for m, i in moves:
            if m >= k * bar_sigma:
                return i
    return max(moves)[1]


def classify(coin_stats, premium_shift_pct=None, oi_chg_pct=None, macro_hits=None,
             news_hits=None, missing=None):
    """coin_stats: {sym: {ret, beta, resid, resid_z, onset_min}}，必含 BTCUSDT。
    回傳 {verdict, layers[], outliers[], confidence, ...}。"""
    btc = coin_stats["BTCUSDT"]
    sign = 1 if btc["ret"] >= 0 else -1
    others = {k: v for k, v in coin_stats.items() if k != "BTCUSDT"}

    same_dir = 0
    for v in others.values():
        implied = (v["beta"] if v["beta"] is not None else 1.0) * btc["ret"]
        if v["ret"] * sign > 0 and abs(v["ret"]) >= 0.5 * abs(implied):
            same_dir += 1
    ratio = same_dir / len(others) if others else 0.0

    onsets = [v["onset_min"] for v in coin_stats.values() if v["onset_min"] is not None]
    spread = (max(onsets) - min(onsets)) if onsets else None
    sync = spread is not None and spread <= SYNC_MAX_MIN

    outliers = sorted(
        [k for k, v in others.items()
         if v["resid_z"] is not None and v["resid_z"] * sign >= RESID_Z],
        key=lambda k: -abs(others[k]["resid_z"]))
    laggards = [k for k, v in others.items()
                if v["resid_z"] is not None and v["resid_z"] * sign <= -RESID_Z]

    if ratio >= SAME_DIR_HI and sync:
        verdict = "全市場（總經／風險偏好）"
    elif ratio < SAME_DIR_LO:
        verdict = "BTC 個別"
    else:
        verdict = "混合（部分同步）"

    layers = []
    if outliers:
        layers.append(f"個別幣多{'漲' if sign > 0 else '跌'}一截：{', '.join(outliers)}")
    if laggards:
        layers.append(f"相對抗{'漲' if sign > 0 else '跌'}：{', '.join(laggards)}")
    if premium_shift_pct is not None and abs(premium_shift_pct) >= PREMIUM_SHIFT_PCT:
        layers.append(f"交易所特定：幣安相對 Coinbase 價差偏移 {premium_shift_pct:+.2f}%")
    if oi_chg_pct is not None and oi_chg_pct <= OI_DROP_PCT:
        layers.append(f"槓桿清算成分：BTC 永續未平倉量 {oi_chg_pct:+.1f}%")
    if macro_hits:
        layers.append("時點重疊：" + "；".join(macro_hits))
    if news_hits:
        layers.append(f"新聞標題線索 {len(news_hits)} 則（內文未查證）")

    n_missing = len(missing or [])
    confidence = "高" if n_missing == 0 else ("中" if n_missing <= 2 else "低")
    return {"verdict": verdict, "same_dir_ratio": round(ratio, 2),
            "onset_spread_min": spread, "sync": sync, "outliers": outliers,
            "laggards": laggards, "layers": layers, "confidence": confidence,
            "missing": list(missing or [])}


# ════════════════════════════════════════════════════════════════
# 資料抓取（失敗一律回 None，由呼叫端列入 missing）
# ════════════════════════════════════════════════════════════════

def _get(url, params=None, timeout=10):
    from core.http_client import safe_get
    return safe_get(url, params=params, timeout=timeout, retries=1)


def fetch_klines(sym, interval, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        r = _get("https://api.binance.com/api/v3/klines",
                 {"symbol": sym, "interval": interval, "startTime": cur,
                  "endTime": end_ms, "limit": 1000})
        rows = r.json()
        if not rows:
            break
        out.extend(rows)
        cur = rows[-1][0] + 1
        if len(rows) < 1000:
            break
    return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
             "c": float(x[4]), "v": float(x[5])} for x in out]


def fetch_coinbase(start, end):
    r = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
             {"granularity": BAR_MIN * 60, "start": start.isoformat(), "end": end.isoformat()})
    # [time, low, high, open, close, volume]，新→舊
    return {int(x[0]) * 1000: float(x[4]) for x in r.json()}


def fetch_oi(start_ms, end_ms):
    r = _get("https://fapi.binance.com/futures/data/openInterestHist",
             {"symbol": "BTCUSDT", "period": "15m", "startTime": start_ms,
              "endTime": end_ms, "limit": 500})
    return [(int(x["timestamp"]), float(x["sumOpenInterest"])) for x in r.json()]


def fetch_funding():
    r = _get("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 3})
    return [(int(x["fundingTime"]), float(x["fundingRate"]) * 100) for x in r.json()]


def fetch_yahoo(symbol, start_ms, end_ms):
    # 15m 資料 Yahoo 最多回溯約 60 天；事件超過 5 天前就改抓 1 個月
    age_days = (datetime.now(timezone.utc).timestamp() * 1000 - start_ms) / 86_400_000
    r = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
             {"interval": "15m", "range": "5d" if age_days < 4 else "1mo"})
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    cl = res["indicators"]["quote"][0]["close"]
    return [(t * 1000, c) for t, c in zip(ts, cl) if c is not None and start_ms <= t * 1000 <= end_ms]


def fetch_news(t0, t1):
    feeds = [("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
             ("Cointelegraph", "https://cointelegraph.com/rss"),
             ("Decrypt", "https://decrypt.co/feed")]
    hits, ok = [], 0
    for name, url in feeds:
        try:
            root = ET.fromstring(_get(url, timeout=8).content)
            ok += 1
        except Exception:
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            except Exception:
                continue
            if t0 <= pub <= t1 and any(k in title.lower() for k in NEWS_KEYWORDS):
                hits.append((pub, name, title))
    return (sorted(hits) if ok else None)


def load_macro_events():
    try:
        with open(os.path.join(_REPO, "db", "macro_events.json"), encoding="utf-8") as f:
            return json.load(f).get("events", [])
    except Exception:
        return None


def us_session_marks(day_utc):
    """回傳該 UTC 日附近的美股現貨開盤／收盤 UTC 時刻（依美東夏令時間）。"""
    try:
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
    except Exception:
        return []
    marks = []
    for d in (day_utc - timedelta(days=1), day_utc, day_utc + timedelta(days=1)):
        if d.weekday() >= 5:
            continue
        for hh, mm, label in ((9, 30, "美股開盤"), (16, 0, "美股收盤")):
            local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ny)
            marks.append((local.astimezone(timezone.utc), label))
    return marks


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

def run(end_tpe=None, hours=24):
    end = (end_tpe or datetime.now(TPE)).astimezone(timezone.utc)
    start = end - timedelta(hours=hours)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    h_ms = int((start - timedelta(days=HIST_DAYS)).timestamp() * 1000)
    missing = []

    bar_ms = BAR_MIN * 60_000
    pre_ms = PRE_HOURS * 3_600_000      # 視窗前多抓，給量能基準與價差基準用（事件可能緊貼視窗開頭）
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    bars, hist = {}, {}
    for sym in COINS:
        try:
            raw = fetch_klines(sym, f"{BAR_MIN}m", s_ms - pre_ms, e_ms)
            # 丟掉還沒收完的 K 棒（盤中值會被當成事件終點）
            bars[sym] = [b for b in raw if b["t"] + bar_ms <= min(e_ms, now_ms)]
            hist[sym] = {b["t"]: b["c"] for b in fetch_klines(sym, "1h", h_ms, s_ms)}
        except Exception as e:
            if sym == "BTCUSDT":
                raise SystemExit(f"BTC K 線抓取失敗，無法歸因：{e}")
            missing.append(f"{sym} K 線")
    btc = bars["BTCUSDT"]
    k0 = next((k for k, b in enumerate(btc) if b["t"] >= s_ms), len(btc))
    i0, i1, btc_pct = find_event([b["c"] for b in btc[k0:]])
    if i0 == i1:
        raise SystemExit("視窗內沒有任何收盤價變動，無事件可歸因")
    i0, i1 = i0 + k0, i1 + k0
    t0, t1 = btc[i0]["t"], btc[i1]["t"]
    n_hours = max((t1 - t0) / 3_600_000, BAR_MIN / 60)

    btc_hr = hourly_returns(hist["BTCUSDT"])
    btc_ret = btc[i1]["c"] / btc[i0]["c"] - 1
    stats = {}
    for sym, bs in bars.items():
        idx = {b["t"]: k for k, b in enumerate(bs)}
        if t0 not in idx or t1 not in idx:
            missing.append(f"{sym} 事件區間缺 bar")
            continue
        j0, j1 = idx[t0], idx[t1]
        cl = [b["c"] for b in bs]
        ret = cl[j1] / cl[j0] - 1
        if sym == "BTCUSDT":
            beta, sd = 1.0, None
        else:
            beta, sd = beta_and_resid_std(hourly_returns(hist.get(sym, {})), btc_hr)
        resid = ret - (beta if beta is not None else 1.0) * btc_ret
        rz = resid / (sd * math.sqrt(n_hours)) if sd else None
        hr = list(hourly_returns(hist.get(sym, {})).values())
        hr_sd = math.sqrt(sum(x * x for x in hr) / len(hr)) if hr else None
        on = onset_index(cl, j0, j1, hr_sd * math.sqrt(BAR_MIN / 60) if hr_sd else None)
        vol_base = sum(b["v"] for b in bs[max(0, j0 - 20):j0]) / max(1, min(20, j0))
        vol_peak = max(b["v"] for b in bs[j0:j1 + 1])
        stats[sym] = {"ret": ret, "beta": beta, "resid": resid,
                      "resid_z": rz, "onset_min": (bs[on]["t"] - t0) / 60000 if on is not None else None,
                      "onset_t": bs[on]["t"] if on is not None else None,
                      "vol_x": vol_peak / vol_base if vol_base else None}

    premium_shift = None
    try:
        cb = fetch_coinbase(datetime.fromtimestamp(t0 / 1000, timezone.utc) - timedelta(hours=2),
                            datetime.fromtimestamp(t1 / 1000, timezone.utc))
        # 基準期＝事件起點「之前」的 K 棒（至少 4 根），不可混入事件中的 bar
        base = [(b["c"] / cb[b["t"]] - 1) * 100 for b in btc if b["t"] < t0 and b["t"] in cb][-8:]
        at_t1 = [(b["c"] / cb[b["t"]] - 1) * 100 for b in btc if b["t"] == t1 and b["t"] in cb]
        if len(base) >= 4 and at_t1:
            premium_shift = at_t1[0] - sum(base) / len(base)
        else:
            missing.append("Coinbase 價差（事件前基準不足 4 根）")
    except Exception:
        missing.append("Coinbase 價差")

    oi_chg = None
    try:
        oi = fetch_oi(t0 - 3_600_000, t1 + 3_600_000)
        # 價格起點＝t0 那根的收盤（t0+15m），OI 也對齊到同一個時刻；OI 時間戳語意未查證
        before = [v for t, v in oi if t <= t0 + bar_ms]
        after = [v for t, v in oi if t <= t1 + bar_ms]
        if before and after:
            oi_chg = (after[-1] / before[-1] - 1) * 100
        else:
            missing.append("BTC 永續未平倉量（區間無資料，API 只回溯 30 天）")
    except Exception:
        missing.append("BTC 永續未平倉量")

    funding = None
    try:
        funding = fetch_funding()
    except Exception:
        missing.append("資金費率")

    macro_hits = []
    t0_dt = datetime.fromtimestamp(t0 / 1000, timezone.utc)
    onset_btc = stats["BTCUSDT"]["onset_t"]
    on_dt = datetime.fromtimestamp((onset_btc or t0) / 1000, timezone.utc)
    events = load_macro_events()
    if events is None:
        missing.append("總經事件表")
    else:
        for ev in events:
            try:
                d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if abs((d - on_dt.date()).days) <= 1:
                macro_hits.append(f"{ev['type']} {ev['date']}")
    for mt, label in us_session_marks(on_dt):
        if abs((mt - t0_dt).total_seconds()) <= 1800 or abs((mt - on_dt).total_seconds()) <= 1800:
            macro_hits.append(f"{label} {mt.astimezone(TPE):%m-%d %H:%M}（台北）")

    tradfi = {}
    for ysym, name in (("ES=F", "標普期貨"), ("DX-Y.NYB", "美元指數"), ("^TNX", "美債10Y")):
        try:
            px = fetch_yahoo(ysym, t0, t1)   # Yahoo 時間戳是開盤時間，與 BTC 的 t0→t1 同口徑
            if len(px) < 2:
                tradfi[name] = None
            elif ysym == "^TNX":   # 報價本身就是殖利率 %，報點數差（bp），不報相對變化
                tradfi[name] = ((px[-1][1] - px[0][1]) * 100, "bp")
            else:
                tradfi[name] = ((px[-1][1] / px[0][1] - 1) * 100, "%")
        except Exception:
            missing.append(f"{name}（Yahoo）")

    news = None
    try:
        news = fetch_news(t0_dt - timedelta(hours=2), end)
    except Exception:
        pass
    if news is None:
        missing.append("新聞 RSS")

    verdict = classify(stats, premium_shift, oi_chg, macro_hits, news, missing)
    return {"window": (start, end), "event": (t0, t1, btc_pct), "stats": stats,
            "premium_shift": premium_shift, "oi_chg": oi_chg, "funding": funding,
            "tradfi": tradfi, "news": news or [], "macro_hits": macro_hits,
            "result": verdict}


def _fmt_t(ms):
    return datetime.fromtimestamp(ms / 1000, TPE).strftime("%m-%d %H:%M")


def _fmt_close(ms):
    """K 棒開盤時間戳 → 收盤時刻（事件價格是收盤價，顯示要對得上）。"""
    return _fmt_t(ms + BAR_MIN * 60_000)


def render(r):
    t0, t1, pct = r["event"]
    res = r["result"]
    lines = [
        f"【BTC 異常波動歸因】視窗 {r['window'][0].astimezone(TPE):%m-%d %H:%M} ~ "
        f"{r['window'][1].astimezone(TPE):%m-%d %H:%M}（台北）",
        f"事件區間：{_fmt_close(t0)} → {_fmt_close(t1)}　BTC {pct:+.2f}%（15 分 K 收盤價峰谷，時間為收盤時刻）",
        "",
        f"判定：{res['verdict']}　信心：{res['confidence']}",
        f"  同向比例 {res['same_dir_ratio']:.0%}　起動時間差 "
        f"{res['onset_spread_min'] if res['onset_spread_min'] is not None else '—'} 分"
        f"（≤{SYNC_MAX_MIN} 分算同步）",
    ]
    for layer in res["layers"]:
        lines.append(f"  · {layer}")
    lines += ["", "幣別      區間報酬   beta   殘差      殘差z   起動*   量能倍數"]
    for sym, v in r["stats"].items():
        beta = f"{v['beta']:.2f}" if v["beta"] is not None else "—"
        rz = f"{v['resid_z']:+.1f}" if v["resid_z"] is not None else "—"
        on = _fmt_t(v["onset_t"])[6:] if v["onset_t"] else "—"
        vx = f"{v['vol_x']:.1f}x" if v["vol_x"] else "—"
        lines.append(f"{sym[:-4]:<8}{v['ret'] * 100:+8.2f}%  {beta:>5}  {v['resid'] * 100:+7.2f}%  "
                     f"{rz:>6}   {on}   {vx}")
    lines += ["  * 起動＝第一根異常大 K 棒的開盤時刻", "", "疊加線索"]
    ps = r["premium_shift"]
    lines.append(f"  幣安−Coinbase 價差偏移：{ps:+.2f}%" if ps is not None else "  幣安−Coinbase 價差：NOT VERIFIED")
    oc = r["oi_chg"]
    lines.append(f"  BTC 永續未平倉量：{oc:+.1f}%" if oc is not None else "  BTC 永續未平倉量：NOT VERIFIED")
    if r["funding"]:
        lines.append("  資金費率：" + "　".join(f"{_fmt_t(t)} {v:+.4f}%" for t, v in r["funding"]))
    for name, v in r["tradfi"].items():
        if v is None:
            lines.append(f"  {name}：區間內無報價（休市，或超出 Yahoo 約 60 天的回溯範圍）")
        elif v[1] == "bp":
            lines.append(f"  {name}（同區間）：{v[0]:+.1f} bp")
        else:
            lines.append(f"  {name}（同區間）：{v[0]:+.2f}%")
    if r["news"]:
        lines.append("  新聞標題（關鍵字命中，內文未查證）：")
        for pub, src, title in r["news"][-8:]:
            lines.append(f"    {pub.astimezone(TPE):%m-%d %H:%M} {src}：{title}")
    if res["missing"]:
        lines.append("  缺資料（NOT VERIFIED）：" + "、".join(res["missing"]))
    lines += ["", "⚠ 門檻為描述性判讀、未經回測；本表不下操作結論，部位解讀見 sop_status.py。"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="BTC 異常波動歸因")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--end", help="台北時間 'YYYY-MM-DD HH:MM'，預設現在")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    end = datetime.strptime(a.end, "%Y-%m-%d %H:%M").replace(tzinfo=TPE) if a.end else None
    r = run(end, a.hours)
    if a.json:
        r = dict(r, window=[x.isoformat() for x in r["window"]],
                 news=[(p.isoformat(), s, t) for p, s, t in r["news"]])
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    else:
        print(render(r))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
