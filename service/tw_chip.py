"""
service/tw_chip.py
台股籌碼/估值資料層 — 供 watcher 台股逃頂/抄底評分（替代加密的 funding/OI/鏈上）。

資料源（TWSE 官方，市場全量單日檔 → 快取一小時 → filter 單一 symbol）：
  - MI_MARGN  融資融券彙總（融資餘額變化＝散戶槓桿；融券餘額＝放空）
  - T86       三大法人買賣超（外資/投信/三大法人；機構派發 vs 吸籌）
  - BWIBBU    個股本益比/股價淨值比/殖利率（估值）

⚠️ 設計取捨（鏡像 tw_stock_climber 概念但**不 import**，Cow 保持自包含、雲端可跑）：
  - TWSE 端點都是「市場全量單日檔」，非個股查詢 → 抓整檔快取，filter 該 symbol。
  - 上市（TWSE）三源完整；上櫃（TPEx）融資融券/法人另有端點，本益比端點未定 →
    上櫃估值維度暫 graceful-None（P1a 先上市完整，上櫃為後續）。
  - 抓不到的源回 None → 評分自動灰燈、不 crash。
  - Accept-Encoding 不要 br（TWSE T86 回 brotli 會解碼錯，requests 無 brotli 時壞）。
  - 融資融券單一回應即含「前日餘額＋今日餘額」→ 變化免多日累積；三大法人為單日買賣超。
"""
import io
import re
import time
import datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TWSE = "https://www.twse.com.tw/rwd/zh"
_TDCC = "https://www.tdcc.com.tw"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"}  # 勿 br
_CACHE_TTL = 3600  # 市場全量檔每小時抓一次（比照 watcher 日線刷新）

# TDCC 集保分級門檻（鏡像 tw_stock_climber，單位：股）
_MAJOR_MIN_SHARES = 1_000_000   # 大戶 ≥ 1000 張
_MID_MIN_SHARES = 400_000       # 中實戶 ≥ 400 張
_RETAIL_MAX_SHARES = 50_000     # 散戶 ≤ 50 張
_TDCC_PUBLISH_LAG_DAYS = 7      # 集保表基準日（週五）公布延遲緩衝

# 市場全量檔快取：{(endpoint, date): (ts, {symbol: row})}
_cache: dict = {}
# TDCC 週資料快取：{(symbol, date_str): result|None}（同檔週資料永久不重抓）
_tdcc_cache: dict = {}


def _session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers.update(_HEADERS)
    return s


def _num(v):
    """TWSE 數字字串 → float。處理千分位逗號/空白/破折號/空值 → None。"""
    if v is None:
        return None
    sv = str(v).strip().replace(",", "")
    if sv in ("", "-", "--", "X", "N/A"):
        return None
    try:
        return float(sv)
    except ValueError:
        return None


def _fetch_market_file(endpoint: str, params: dict, key_idx: int = 0) -> dict:
    """
    抓 TWSE 市場全量單日檔 → {symbol: row(list)}，每小時快取。
    自動深找含 fields+data 的 table（TWSE 回應有時包在 tables[]）。失敗回 {}。
    """
    date = params.get("date", "")
    ck = (endpoint, date)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    try:
        r = _session().get(f"{_TWSE}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[tw_chip] {endpoint} 抓取失敗：{e}")
        return {}

    def _find_table(obj):
        if isinstance(obj, dict):
            if "fields" in obj and "data" in obj:
                return obj
            for v in obj.values():
                t = _find_table(v)
                if t:
                    return t
        elif isinstance(obj, list):
            for v in obj:
                t = _find_table(v)
                if t:
                    return t
        return None

    table = _find_table(j) or {}
    data = table.get("data", []) or []
    out = {str(row[key_idx]).strip(): row for row in data if row and len(row) > key_idx}
    _cache[ck] = (time.time(), out)
    return out


def get_margin(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    融資融券（MI_MARGN）。回傳該檔 {fin_balance, fin_prev, fin_chg_lots,
    short_balance, short_prev}（單位：張）；查無回 None。
    融資餘額增＝散戶加槓桿（過熱）；融資大減＝斷頭清洗（抄底）；融券回補亦為訊號。
    """
    rows = _fetch_market_file(
        "marginTrading/MI_MARGN",
        {"date": date_yyyymmdd, "selectType": "STOCK", "response": "json"})
    row = rows.get(symbol)
    if not row or len(row) < 13:
        return None
    fin_prev, fin_today = _num(row[5]), _num(row[6])         # 融資 前日/今日餘額
    short_prev, short_today = _num(row[11]), _num(row[12])   # 融券 前日/今日餘額
    if fin_today is None:
        return None
    return {
        "fin_balance": fin_today, "fin_prev": fin_prev,
        "fin_chg_lots": (fin_today - fin_prev) if fin_prev is not None else None,
        "fin_chg_pct": ((fin_today / fin_prev - 1) * 100) if fin_prev else None,
        "short_balance": short_today, "short_prev": short_prev,
        "short_chg_lots": (short_today - short_prev) if (short_today is not None and short_prev is not None) else None,
    }


def get_institutional(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    三大法人買賣超（T86，單位：股）。回傳 {foreign_net, trust_net, dealer_net, total_net}；
    正＝買超（吸籌）、負＝賣超（派發）。查無回 None。
    """
    rows = _fetch_market_file(
        "fund/T86",
        {"date": date_yyyymmdd, "selectType": "ALLBUT0999", "response": "json"})
    row = rows.get(symbol)
    if not row or len(row) < 19:
        return None
    return {
        "foreign_net": _num(row[4]),    # 外陸資買賣超（不含外資自營商）
        "trust_net": _num(row[10]),     # 投信買賣超
        "dealer_net": _num(row[11]),    # 自營商買賣超（合計）
        "total_net": _num(row[18]),     # 三大法人買賣超
    }


def get_valuation(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    本益比/股價淨值比/殖利率（BWIBBU，上市）。回傳 {pe, pb, yield_pct, close}；
    上櫃股不在 TWSE 此檔 → 回 None（估值維度灰燈）。
    """
    rows = _fetch_market_file(
        "afterTrading/BWIBBU_d",
        {"date": date_yyyymmdd, "selectType": "ALL", "response": "json"})
    row = rows.get(symbol)
    if not row or len(row) < 7:
        return None
    return {"close": _num(row[2]), "yield_pct": _num(row[3]),
            "pe": _num(row[5]), "pb": _num(row[6])}


# ── TDCC 集保大戶分布（鏡像 tw_stock_climber 的 GET→POST CSRF 爬法）─────────────
def latest_tdcc_friday(now: datetime.date = None) -> str:
    """最近一個「已公布」的集保基準週五（今天扣公布延遲後往前找週五）→ YYYYMMDD。"""
    d = (now or datetime.date.today()) - datetime.timedelta(days=_TDCC_PUBLISH_LAG_DAYS)
    d -= datetime.timedelta(days=(d.weekday() - 4) % 7)   # 回退到該週或前一個週五
    return d.strftime("%Y%m%d")


def _parse_share_range(text: str):
    """持股分級字串 → (lower, upper) 股數。如 '1,000,001以上' / '1-999' / '1,000-5,000'。"""
    t = str(text).replace(",", "").strip()
    if "以上" in t:
        m = re.search(r"(\d+)", t)
        return (int(m.group(1)), 10 ** 12) if m else (0, 0)
    m = re.match(r"(\d+)\s*[-~]\s*(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    return (0, 0)


def get_tdcc(symbol: str, date_str: str = None) -> dict | None:
    """
    TDCC 集保大戶/中實戶/散戶持股比例（週五基準）。回傳
    {date, major_pct, mid_pct, retail_pct} 或 None。記憶體快取，同週同檔不重抓。
    大戶持股比上升＝吸籌（抄底加分）；散戶比上升＝派發。
    """
    date_str = date_str or latest_tdcc_friday()
    ck = (symbol, date_str)
    if ck in _tdcc_cache:
        return _tdcc_cache[ck]

    result = None
    try:
        import pandas as pd
        s = _session()
        url = f"{_TDCC}/portal/zh/smWeb/qryStock"
        r1 = s.get(url, timeout=15)
        r1.raise_for_status()
        tok = re.search(r'name="SYNCHRONIZER_TOKEN"[^>]*value="([^"]+)"', r1.text)
        uri = re.search(r'name="SYNCHRONIZER_URI"[^>]*value="([^"]+)"', r1.text)
        if not tok:
            raise ValueError("CSRF token 未找到")
        payload = {
            "SYNCHRONIZER_TOKEN": tok.group(1),
            "SYNCHRONIZER_URI": uri.group(1) if uri else "/portal/zh/smWeb/qryStock",
            "method": "submit", "firDate": date_str, "scaDate": date_str,
            "sqlMethod": "StockNo", "stockNo": symbol, "stockName": "",
        }
        r2 = s.post(url, data=payload, timeout=15)
        r2.raise_for_status()
        for tbl in pd.read_html(io.StringIO(r2.text)):
            if len(tbl) < 5:
                continue
            range_col = next((c for c in tbl.columns
                              if tbl[c].astype(str).str.contains(r"[-~以上]", regex=True).sum() >= 5), None)
            if range_col is None:
                continue
            pct_col = next((c for c in tbl.columns
                            if c != range_col and ("比例" in str(c) or "%" in str(c))), None)
            if pct_col is None:
                continue
            major = mid = retail = 0.0
            for _, row in tbl.iterrows():
                lo, hi = _parse_share_range(row[range_col])
                if lo == 0 and hi == 0:
                    continue
                try:
                    pct = float(str(row[pct_col]).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if lo >= _MAJOR_MIN_SHARES:
                    major += pct
                elif lo >= _MID_MIN_SHARES or lo > _RETAIL_MAX_SHARES:
                    mid += pct
                elif hi <= _RETAIL_MAX_SHARES:
                    retail += pct
            if major > 0 or mid > 0 or retail > 0:
                result = {"date": date_str, "major_pct": round(major, 2),
                          "mid_pct": round(mid, 2), "retail_pct": round(retail, 2)}
                break
    except Exception as e:  # noqa: BLE001
        print(f"[tw_chip] TDCC 抓取失敗（{symbol}）：{e}")

    _tdcc_cache[ck] = result
    return result


def get_chip_bundle(symbol: str, date_yyyymmdd: str, lookback: int = 7) -> dict:
    """
    一次取齊四源（每源獨立 best-effort，抓不到的為 None）。回傳
    {margin, institutional, valuation, tdcc, as_of}，供台股逃頂/抄底評分注入。

    ⚠️ TWSE 日檔為 EOD 公布：呼叫端常傳「今日」（Yahoo 最後日線可能是盤中/未收的今天），
    但今日 EOD 檔尚未出 → 會整片 None。故從 date 往前找「最近有公布的交易日」（最多 lookback 天，
    跳過週末/未公布日），三個日檔（融資/法人/估值）對齊同一 as_of 日。TDCC 為週資料另解。
    """
    # 先用單一端點（BWIBBU 市場檔非空）探「最近已公布的交易日」→ 再抓三源（BWIBBU 已快取）。
    # 避免一次猛打多源×多日撞 TWSE 限流（端午等連假/今日未收時尤需 walk back）。
    d = datetime.datetime.strptime(date_yyyymmdd, "%Y%m%d").date()
    as_of = date_yyyymmdd
    for _ in range(max(1, lookback)):
        ds = d.strftime("%Y%m%d")
        if _fetch_market_file("afterTrading/BWIBBU_d",
                              {"date": ds, "selectType": "ALL", "response": "json"}):
            as_of = ds
            break
        d -= datetime.timedelta(days=1)
    return {"margin": get_margin(symbol, as_of),
            "institutional": get_institutional(symbol, as_of),
            "valuation": get_valuation(symbol, as_of),
            "tdcc": get_tdcc(symbol), "as_of": as_of}
