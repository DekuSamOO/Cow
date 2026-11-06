"""
service/tw_chip.py
台股籌碼/估值資料層 — 供 watcher 台股逃頂/抄底評分（替代加密的 funding/OI/鏈上）。

資料源（TWSE 官方，市場全量單日檔 → 快取一小時 → filter 單一 symbol）：
  - MI_MARGN  融資融券彙總（融資餘額變化＝散戶槓桿；融券餘額＝放空）
  - T86       三大法人買賣超（外資/投信/三大法人；機構派發 vs 吸籌）
  - BWIBBU    個股本益比/股價淨值比/殖利率＋**財報年/季**（估值與其基期）
  - t187ap05  每月營收彙總（OpenAPI 家族，**僅最新一期、無歷史** → 見 get_monthly_revenue）

⚠️ 設計取捨（鏡像 tw_stock_climber 概念但**不 import**，Cow 保持自包含、雲端可跑）：
  - TWSE/TPEx 端點都是「市場全量單日檔」，非個股查詢 → 抓整檔快取，filter 該 symbol。
  - 估值/融資/法人三日檔皆「上市 TWSE + 上櫃 TPEx fallback」（_fetch_market_file base=_TPEX）：
    估值 BWIBBU→peQryDate、融資 MI_MARGN→margin/balance、法人 T86→insti/dailyTrade，
    上市查無（上櫃股）即轉打對應 TPEx 端點 → 上櫃四維（融資/法人/估值/大戶）皆可用。
  - 抓不到的源回 None → 評分自動灰燈、不 crash。
  - Accept-Encoding 不要 br（TWSE T86 回 brotli 會解碼錯，requests 無 brotli 時壞）。
  - 融資融券單一回應即含「前日餘額＋今日餘額」→ 變化免多日累積；三大法人為單日買賣超。
"""
import io
import os
import re
import time
import datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TWSE = "https://www.twse.com.tw/rwd/zh"
_TPEX = "https://www.tpex.org.tw"          # 上櫃（估值 fallback）
_TDCC = "https://www.tdcc.com.tw"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"}  # 勿 br
_CACHE_TTL = 3600  # 市場全量檔每小時抓一次（比照 watcher 日線刷新）

# 已發行股數（週轉率用）：TWSE/TPEx OpenAPI，與上面 rwd/zh 系列不同 API 家族、無 date 參數。
_TWSE_OPEN_T187 = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
_TPEX_OPEN_T187 = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
_SHARES_TTL = 86400  # 股本變動極不頻繁 → 日快取（非每小時）；全市場回應大，抓取實測 20-45s

# 每月營收（MOPS 月營收彙總）：同上 OpenAPI 家族、同樣無 date 參數 → 只有「最新一期」。
# TWSE 與 TPEx 兩端點的 JSON key 完全相同（2026-08-12 實測 1069 / 890 筆），故共用一套解析。
_TWSE_OPEN_REV = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
_TPEX_OPEN_REV = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
_REV_TTL = 86400  # 月更資料，日快取；每小時重抓純粹浪費

# TDCC 集保分級門檻（鏡像 tw_stock_climber，單位：股）
_MAJOR_MIN_SHARES = 1_000_000   # 大戶 ≥ 1000 張
_MID_MIN_SHARES = 400_000       # 中實戶 ≥ 400 張
_RETAIL_MAX_SHARES = 50_000     # 散戶 ≤ 50 張
_TDCC_PUBLISH_LAG_DAYS = 7      # 集保表基準日（週五）公布延遲緩衝

# 市場全量檔快取：{(endpoint, date): (ts, {symbol: row})}
_cache: dict = {}
# TDCC 週資料快取：{(symbol, date_str): result|None}（同檔週資料永久不重抓）
_tdcc_cache: dict = {}
# 已發行股數快取：{base_url: (ts, {symbol: shares})}
_shares_cache: dict = {}
# 月營收快取：{base_url: (ts, {symbol: row(dict)})}
_rev_cache: dict = {}


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


def _tpex_date(date_yyyymmdd: str) -> str:
    """YYYYMMDD → TPEx 端點要的 yyyy/mm/dd。"""
    return f"{date_yyyymmdd[:4]}/{date_yyyymmdd[4:6]}/{date_yyyymmdd[6:8]}"


# 民國年轉換：三個端點三種格式，各自的 raw 都一併保留給呼叫端，解析不出來一律回 None（不猜）。
_ROC_Q_RE = re.compile(r"^(\d{2,3})\s*[/QqＱ]\s*([1-4])$")


def _roc_quarter(raw) -> str | None:
    """民國財報年季 → 西元 `2026Q2`。TWSE 給 `115/2`、TPEx 給 `115Q1`，兩種都收。"""
    if raw is None:
        return None
    m = _ROC_Q_RE.match(str(raw).strip())
    return f"{int(m.group(1)) + 1911}Q{m.group(2)}" if m else None


def _roc_ym(raw) -> str | None:
    """民國年月 `11507` → `2026-07`。"""
    s = str(raw).strip() if raw is not None else ""
    if not s.isdigit() or not 5 <= len(s) <= 6:
        return None
    return f"{int(s[:-2]) + 1911}-{s[-2:]}"


def _roc_ymd(raw) -> str | None:
    """民國年月日 `1150811` → `2026-08-11`。"""
    s = str(raw).strip() if raw is not None else ""
    if not s.isdigit() or not 6 <= len(s) <= 7:
        return None
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"


def _fetch_market_file(endpoint: str, params: dict, key_idx: int = 0, base: str = _TWSE) -> dict:
    """
    抓 TWSE/TPEx 市場全量單日檔 → {symbol: row(list)}，每小時快取。
    自動深找含 fields+data 的 table（回應有時包在 tables[]）。失敗回 {}。
    base 預設 TWSE（上市）；上櫃傳 _TPEX。
    """
    date = params.get("date", "")
    ck = (base, endpoint, date)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    try:
        r = _session().get(f"{base}/{endpoint}", params=params, timeout=20)
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
    上市走 TWSE MI_MARGN；上市查無（上櫃股）→ fallback TPEx margin/balance。
    """
    rows = _fetch_market_file(
        "marginTrading/MI_MARGN",
        {"date": date_yyyymmdd, "selectType": "STOCK", "response": "json"})
    row = rows.get(symbol)
    if row and len(row) >= 13:
        return _margin_dict(_num(row[5]), _num(row[6]), _num(row[11]), _num(row[12]))
    return _get_margin_tpex(symbol, date_yyyymmdd)


def _margin_dict(fin_prev, fin_today, short_prev, short_today) -> dict | None:
    """融資融券前日/今日餘額（張）→ 統一回傳 dict（TWSE/TPEx 共用）。"""
    if fin_today is None:
        return None
    return {
        "fin_balance": fin_today, "fin_prev": fin_prev,
        "fin_chg_lots": (fin_today - fin_prev) if fin_prev is not None else None,
        "fin_chg_pct": ((fin_today / fin_prev - 1) * 100) if fin_prev else None,
        "short_balance": short_today, "short_prev": short_prev,
        "short_chg_lots": (short_today - short_prev) if (short_today is not None and short_prev is not None) else None,
    }


def _get_margin_tpex(symbol: str, date_yyyymmdd: str) -> dict | None:
    """上櫃融資融券（TPEx margin/balance）。欄序：2前資餘額/6資餘額/10前券餘額/14券餘額（張）。"""
    rows = _fetch_market_file("www/zh-tw/margin/balance",
                              {"date": _tpex_date(date_yyyymmdd), "response": "json"}, base=_TPEX)
    row = rows.get(symbol)
    if not row or len(row) < 15:
        return None
    return _margin_dict(_num(row[2]), _num(row[6]), _num(row[10]), _num(row[14]))


def get_institutional(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    三大法人買賣超（T86，單位：股）。回傳 {foreign_net, trust_net, dealer_net, total_net}；
    正＝買超（吸籌）、負＝賣超（派發）。查無回 None。
    上市走 TWSE T86；上市查無（上櫃股）→ fallback TPEx insti/dailyTrade。
    """
    rows = _fetch_market_file(
        "fund/T86",
        {"date": date_yyyymmdd, "selectType": "ALLBUT0999", "response": "json"})
    row = rows.get(symbol)
    if row and len(row) >= 19:
        return {
            "foreign_net": _num(row[4]),    # 外陸資買賣超（不含外資自營商）
            "trust_net": _num(row[10]),     # 投信買賣超
            "dealer_net": _num(row[11]),    # 自營商買賣超（合計）
            "total_net": _num(row[18]),     # 三大法人買賣超
        }
    return _get_institutional_tpex(symbol, date_yyyymmdd)


def _get_institutional_tpex(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    上櫃三大法人（TPEx insti/dailyTrade，單位：股）。24 欄、每型 買進/賣出/買賣超 三欄：
    4外陸資(不含自營)買賣超 / 13投信買賣超 / 22自營商合計 / 23三大法人合計。評分只用 total_net。
    """
    rows = _fetch_market_file("www/zh-tw/insti/dailyTrade",
                              {"date": _tpex_date(date_yyyymmdd), "type": "Daily", "response": "json"}, base=_TPEX)
    row = rows.get(symbol)
    if not row or len(row) < 24:
        return None
    return {"foreign_net": _num(row[4]), "trust_net": _num(row[13]),
            "dealer_net": _num(row[22]), "total_net": _num(row[23])}


def get_valuation(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    本益比/股價淨值比/殖利率。回傳 {pe, pb, yield_pct, close,
    pe_fiscal_quarter, pe_fiscal_quarter_raw}；查無回 None。
    上市走 TWSE BWIBBU（欄 0代號 1名稱 2收盤價 3殖利率% 4股利年度 5本益比 6PB 7財報年/季）；
    上市查無（上櫃股）→ fallback TPEx peQryDate。
    注意：TPEx 分支 close 為 None（peQryDate 無收盤價欄），呼叫端勿對 close 做算術。

    `pe_fiscal_quarter`（2026-08-12 新增）＝這個 PE 的**近四季（TTM）EPS 截止於哪一季**。
    **不是**「拿那一季的 EPS 算的」——分母是四季和，本欄只標那個窗口的最後一季。
    2026-08-13 兩檔對拍（收盤 ÷ PE ＝ 隱含 EPS，再與外部近四季 EPS 比對）：
      6782 `115/2`：207.5 ÷ 12.58 ＝ 16.49 ＝ 4.06+3.98+3.82+4.63 ✅
      2330 `115/1`：2415.0 ÷ 32.47 ＝ 74.38 ＝ 15.36+17.44+19.51+22.08 ✅
      反證：2330 單季 22.08 年化（88.32）→ PE 27.34、單季本身 → 109.4，都對不上。
    同一天不同股票的截止季可以不同（上面兩檔就差一季）。沒有這一欄，「PE 13 不貴」
    查不出便宜在哪個窗口上；循環股 EPS 高峰時 PE 看起來最低（那是賣點不是買點），
    TTM 只把這個失真平滑掉一部分、**不會消除**。欄位缺漏時回 None，不推測。
    """
    rows = _fetch_market_file(
        "afterTrading/BWIBBU_d",
        {"date": date_yyyymmdd, "selectType": "ALL", "response": "json"})
    row = rows.get(symbol)
    if row and len(row) >= 7:
        # 財報年/季在 idx 7 → 需 len>=8 才讀得到；舊檔若只有 7 欄仍照常回其餘欄位（不因加值欄失效）。
        raw_q = row[7] if len(row) >= 8 else None
        return {"close": _num(row[2]), "yield_pct": _num(row[3]),
                "pe": _num(row[5]), "pb": _num(row[6]),
                "pe_fiscal_quarter": _roc_quarter(raw_q),
                "pe_fiscal_quarter_raw": str(raw_q).strip() if raw_q is not None else None}
    return _get_valuation_tpex(symbol, date_yyyymmdd)


def _get_valuation_tpex(symbol: str, date_yyyymmdd: str) -> dict | None:
    """
    上櫃本益比/股價淨值比/殖利率（TPEx peQryDate）。欄位順序與 TWSE 不同：
    0代號 1名稱 2本益比 3每股股利 4股利年度 5殖利率% 6股價淨值比 7財報季（無收盤價）。
    日期格式 yyyy/mm/dd。財報年/季格式也與 TWSE 不同（TPEx `115Q1`、TWSE `115/2`），
    兩種都由 `_roc_quarter` 吸收。
    """
    rows = _fetch_market_file(
        "www/zh-tw/afterTrading/peQryDate",
        {"date": _tpex_date(date_yyyymmdd), "response": "json"}, base=_TPEX)
    row = rows.get(symbol)
    if not row or len(row) < 7:
        return None
    raw_q = row[7] if len(row) >= 8 else None
    return {"close": None, "yield_pct": _num(row[5]),
            "pe": _num(row[2]), "pb": _num(row[6]),
            "pe_fiscal_quarter": _roc_quarter(raw_q),
            "pe_fiscal_quarter_raw": str(raw_q).strip() if raw_q is not None else None}


# ── 已發行股數（週轉率用；2026-07 新增，資料源見 tests/core/test_relative_universal.py 同批調查）──
def _fetch_shares_outstanding_market(base_url: str, code_key: str, shares_key: str) -> dict:
    """
    抓全市場已發行股數快照（TWSE/TPEx OpenAPI，全量單日檔、無 date 參數），日快取（見
    `_SHARES_TTL`，股本變動極不頻繁，不比照其他籌碼檔每小時重抓）。
    回應體積大（~1MB+，全市場一千多檔），實測耗時 20–45 秒 → timeout 拉長到 60s。
    這個 domain 的 requests 自動編碼偵測常猜錯 → 強制 `r.encoding = "utf-8"`。
    抓取失敗時退回舊快取（若有）而非清空——股本本來就幾乎不變，舊資料仍可信。
    """
    hit = _shares_cache.get(base_url)
    if hit and time.time() - hit[0] < _SHARES_TTL:
        return hit[1]
    try:
        r = _session().get(base_url, timeout=60)
        r.raise_for_status()
        r.encoding = "utf-8"
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[tw_chip] 股本端點抓取失敗（{base_url}）：{e}")
        return hit[1] if hit else {}
    out = {}
    for row in data:
        code = str(row.get(code_key, "")).strip()
        shares = _num(row.get(shares_key))
        if code and shares:
            out[code] = shares
    _shares_cache[base_url] = (time.time(), out)
    return out


def get_shares_outstanding(symbol: str) -> float | None:
    """
    已發行普通股數（股）。用於計算週轉率＝即時成交量÷已發行股數。
    上市查 TWSE OpenAPI（`公司代號`/`已發行普通股數或TDR原股發行股數`）；
    上市查無（上櫃股）→ 轉查 TPEx OpenAPI（`SecuritiesCompanyCode`/`IssueShares`）。查無回 None。
    """
    twse = _fetch_shares_outstanding_market(
        _TWSE_OPEN_T187, "公司代號", "已發行普通股數或TDR原股發行股數")
    if symbol in twse:
        return twse[symbol]
    tpex = _fetch_shares_outstanding_market(_TPEX_OPEN_T187, "SecuritiesCompanyCode", "IssueShares")
    return tpex.get(symbol)


# ── 每月營收（MOPS 月營收彙總；2026-08-12 新增）────────────────────────────────
def _fetch_revenue_market(base_url: str) -> dict:
    """
    抓全市場最新一期月營收快照 → {公司代號: row(dict)}，日快取（見 `_REV_TTL`）。
    與股本端點同 API 家族，故沿用同兩個坑的處理：
      1. 該 domain 的 requests 編碼偵測會猜錯 → 必須強制 `r.encoding = "utf-8"`
      2. 回應為全市場（實測 TWSE 1069／TPEx 890 筆）→ timeout 拉長
    抓取失敗退回舊快取而非清空（同 `_fetch_shares_outstanding_market` 的理由：
    月營收一個月才變一次，舊的仍可信，且欄位本身自帶 `資料年月` 可供呼叫端判斷新舊）。
    """
    hit = _rev_cache.get(base_url)
    if hit and time.time() - hit[0] < _REV_TTL:
        return hit[1]
    try:
        r = _session().get(base_url, timeout=90)
        r.raise_for_status()
        r.encoding = "utf-8"
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[tw_chip] 月營收端點抓取失敗（{base_url}）：{e}")
        return hit[1] if hit else {}
    out = {}
    for row in data:
        code = str(row.get("公司代號", "")).strip()
        if code:
            out[code] = row
    _rev_cache[base_url] = (time.time(), out)
    return out


def get_monthly_revenue(symbol: str) -> dict | None:
    """
    最新一期每月營收（MOPS 月營收彙總）。上市查 TWSE OpenAPI；查無（上櫃股）→ 轉 TPEx。
    查無回 None。

    ⚠️ **端點無 date 參數 → 只有最新一期快照，沒有歷史序列。**
    因此本區塊一律只能當**描述性事實**陳述，**不得餵進逃頂/抄底雷達或任何評分**：
    沒有歷史就跑不出回測，把未回測的維度加權進分數會產生虛假的驗證感
    （CONSTITUTION 第 8-12 條）。真要做序列，得自行逐月歸檔累積，那是另一件事。

    單位：金額欄一律為**仟元**（原始檔即為仟元），故 key 全帶 `_ktwd` 後綴——
    千倍級的單位不寫進欄名，遲早有人當成元。
    成長率一律**照抄來源的 `(%)` 欄**，不由三個金額自行回推（來源自算的才是官方口徑）。
    `published_at`（出表日期）給的是這期資料的公布日，供呼叫端標 PiT 用。
    """
    for src, base in (("TWSE", _TWSE_OPEN_REV), ("TPEx", _TPEX_OPEN_REV)):
        row = _fetch_revenue_market(base).get(symbol)
        if not row:
            continue
        note = str(row.get("備註", "")).strip()
        return {
            "source": src,
            "data_month": _roc_ym(row.get("資料年月")),
            "data_month_raw": str(row.get("資料年月", "")).strip() or None,
            "published_at": _roc_ymd(row.get("出表日期")),
            "published_at_raw": str(row.get("出表日期", "")).strip() or None,
            "company_name": str(row.get("公司名稱", "")).strip() or None,
            "industry": str(row.get("產業別", "")).strip() or None,
            "revenue_ktwd": _num(row.get("營業收入-當月營收")),
            "revenue_prev_month_ktwd": _num(row.get("營業收入-上月營收")),
            "revenue_last_year_ktwd": _num(row.get("營業收入-去年當月營收")),
            "mom_pct": _num(row.get("營業收入-上月比較增減(%)")),
            "yoy_pct": _num(row.get("營業收入-去年同月增減(%)")),
            "cum_revenue_ktwd": _num(row.get("累計營業收入-當月累計營收")),
            "cum_revenue_last_year_ktwd": _num(row.get("累計營業收入-去年累計營收")),
            "cum_yoy_pct": _num(row.get("累計營業收入-前期比較增減(%)")),
            "note": note if note and note != "-" else None,
            "limitation": ("端點僅提供最新一期、無歷史序列 → 描述性事實，"
                           "不可回測、不可入任何評分"),
        }
    return None


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


def _fetch_tdcc_week(symbol: str, date_str: str) -> dict | None:
    """單一週五的 TDCC 集保抓取（GET→POST CSRF）→ dict|None。查無/失敗回 None。記憶體快取同週同檔。"""
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
        print(f"[tw_chip] TDCC 抓取失敗（{symbol} {date_str}）：{e}")

    _tdcc_cache[ck] = result
    return result


def get_tdcc(symbol: str, date_str: str = None, max_back_weeks: int = 4) -> dict | None:
    """
    TDCC 集保大戶/中實戶/散戶持股比例（週五基準）。回傳
    {date, major_pct, mid_pct, retail_pct} 或 None。大戶持股比上升＝吸籌（抄底加分）；散戶比上升＝派發。

    最新週五常「尚未公布」（TDCC 公布有延遲）→ 查無；故自 latest_tdcc_friday() 往前
    最多 max_back_weeks 週，逐週試到抓到已公布資料為止（鏡像 tw_stock_climber preflight 邏輯）。
    傳入明確 date_str 時只查該週、不往前找。
    """
    if date_str:
        return _fetch_tdcc_week(symbol, date_str)
    d = datetime.datetime.strptime(latest_tdcc_friday(), "%Y%m%d").date()
    for _ in range(max(1, max_back_weeks)):
        res = _fetch_tdcc_week(symbol, d.strftime("%Y%m%d"))
        if res:
            return res
        d -= datetime.timedelta(days=7)
    return None


def get_chip_bundle(symbol: str, date_yyyymmdd: str, lookback: int = 7) -> dict:
    """
    一次取齊四源（每源獨立 best-effort，抓不到的為 None）。回傳
    {margin, institutional, valuation, tdcc, as_of}，供台股逃頂/抄底評分注入。

    ⚠️ TWSE 日檔為 EOD 公布：呼叫端常傳「今日」（Yahoo 最後日線可能是盤中/未收的今天），
    但今日 EOD 檔尚未出 → 會整片 None。故從 date 往前找「最近有公布的交易日」（最多 lookback 天，
    跳過週末/未公布日），三個日檔（融資/法人/估值）對齊同一 as_of 日。TDCC 為週資料另解。
    """
    # 探「最近三日檔（估值+融資+法人）都已公布」的交易日 → 再抓三源（探測時已快取，無重抓）。
    # 為何要三檔齊備：三檔公布時間不同步（實測今日 BWIBBU/T86 已出但 MI_MARGN 未出），
    # 若只探 BWIBBU 會把 as_of 鎖在今天 → 融資整片 None。要求三檔皆非空才採用，否則往前一天。
    # 避免一次猛打多源×多日撞 TWSE 限流（端午等連假/今日未收時尤需 walk back）。
    # 估值/融資/法人三檔的探測端點（探測即預熱 _fetch_market_file 快取，採用日不重抓）。
    _probes = (("afterTrading/BWIBBU_d", "ALL"),
               ("marginTrading/MI_MARGN", "STOCK"),
               ("fund/T86", "ALLBUT0999"))
    d = datetime.datetime.strptime(date_yyyymmdd, "%Y%m%d").date()
    as_of = date_yyyymmdd
    for _ in range(max(1, lookback)):
        ds = d.strftime("%Y%m%d")
        if all(_fetch_market_file(ep, {"date": ds, "selectType": st, "response": "json"})
               for ep, st in _probes):
            as_of = ds
            break
        d -= datetime.timedelta(days=1)
    return {"margin": get_margin(symbol, as_of),
            "institutional": get_institutional(symbol, as_of),
            "valuation": get_valuation(symbol, as_of),
            "tdcc": get_tdcc(symbol), "as_of": as_of}


# ── 大盤（上市）融資維持率：watcher 台股面板「僅顯示、不計分」 ─────────────────────
# 定義：Σ(上市全部證券融資今日餘額張 × 1000 × 當日收盤) ÷ 上市融資金額（元）× 100，**含 ETF**。
# 分子分母必須同範圍：融資金額（MI_MARGN「融資金額(仟元)」）本身含 ETF 的融資，分子若只算普通股
# 會低估，且 ETF 融資占比逐年升高（張數占比 2016 約 15% → 2026 約 34%），偏差隨時間變大
# ——2026-11-06 finance-calc-reviewer 抓到，初版就犯了這個錯（11-04：只算股票 155.45%、全部 164.66%）。
# 財經M平方停更前用「不含 ETF 的融資股票市值 ÷ 融資餘額」，正是這個範圍不一致的口徑，數字不可互比；
# 證交所 2026-07-28 澄清的「整戶擔保維持率」（融資＋融券、上市＋上櫃）又是另一個口徑。
# 不計分的理由：tw_stock_climber in-sample 測試（vault Github\tw_stock_climber\歷程\
# 20261106findings_融資維持率對冠軍選股.md）沒有足夠證據，CONSTITUTION 第 11 條不得拿來加權。
#
# 百分位要一年歷史：即時去抓要打上百次 TWSE（撞限流），故用「種子檔＋本機追加檔」：
#   - 種子 db/tw_margin_ratio_seed.csv：2016-01-04~2026-11-05，與本函式**同一口徑**，由 TWSE 官方
#     逐日 MI_MARGN ALL × MI_INDEX ALLBUT0999 算出（tw_stock_climber lab/margin_mr/fetch_twse_full.py，
#     2,632 天 0 失敗；11-04 164.66% 與本函式即時算的相同），**之後不再改**。
#     不用 climber DB：DB 沒有 ETF，且只 join 現存 stock_list，與官方「只算股票」相比差到 ±29pp。
# 已知偏差（往低偏）：停牌／無收盤價的證券，融資金額仍在分母、擔保品卻不進分子。全期無價張數占比
#   中位數 0.017%，但約 67 天 >1%，最大 2024-08-22 新光金＋群創停牌 −2.7pp（2026-11-06 複審量化）。
#   券商端停牌股是否以最後收盤價計維持率未查證，故不補值，只記錄。
#     放 db/ 但內容固定，09:00 自動 commit 不會帶到它。
#   - 本機 data/tw_margin_ratio_live.csv（data/ 在 .gitignore）：watcher 每算出新的一天就追加。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MMR_SEED_PATH = os.path.join(_ROOT, "db", "tw_margin_ratio_seed.csv")
MMR_LIVE_PATH = os.path.join(_ROOT, "data", "tw_margin_ratio_live.csv")
_MMR_PCTILE_WINDOW = 252   # 近一年交易日
_MMR_PCTILE_MIN = 120      # 歷史不足半年不給百分位（避免短母體分位，同 rules No.18 精神）
_mmr_cache: dict = {}      # {(as_of, seed, live): 結果}；同一交易日只算一次（見 get_market_margin_ratio）


def _fetch_twse_tables(endpoint: str, params: dict) -> list:
    """TWSE rwd 回應的 tables[]（失敗回 []），與 `_fetch_market_file` 共用快取 TTL。
    MI_INDEX、MI_MARGN(MS) 的目標表不是第一張，不能用 `_fetch_market_file` 的「找第一張表」。"""
    ck = (_TWSE, endpoint, "tables", tuple(sorted(params.items())))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        r = _session().get(f"{_TWSE}/{endpoint}", params=params, timeout=30)
        r.raise_for_status()
        tables = r.json().get("tables") or []
    except Exception as e:  # noqa: BLE001
        print(f"[tw_chip] {endpoint} 抓取失敗：{e}")
        return []
    _cache[ck] = (time.time(), tables)
    return tables


def _twse_close_map(date_yyyymmdd: str) -> dict:
    """上市個股當日收盤價（MI_INDEX ALLBUT0999 的個股行情表）→ {代號: close}。"""
    tables = _fetch_twse_tables("afterTrading/MI_INDEX",
                                {"date": date_yyyymmdd, "type": "ALLBUT0999", "response": "json"})
    for t in tables:
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ci, pi = f.index("證券代號"), f.index("收盤價")
            return {str(r[ci]).strip(): _num(r[pi]) for r in t.get("data") or [] if len(r) > pi}
    return {}


def _twse_margin_all(date_yyyymmdd: str):
    """MI_MARGN ALL 一次取兩樣：({代號: 融資今日餘額張}, 融資金額仟元)。抓不到回 ({}, None)。
    ALL 回應同時含「信用交易統計」彙總表（融資金額）與逐檔表，不必另打 selectType=MS。"""
    tables = _fetch_twse_tables("marginTrading/MI_MARGN",
                                {"date": date_yyyymmdd, "selectType": "ALL", "response": "json"})
    lots, loan_k = {}, None
    for t in tables:
        f = t.get("fields") or []
        if "今日餘額" not in f:
            continue
        ti = f.index("今日餘額")
        for r in t.get("data") or []:
            if not r or len(r) <= ti:
                continue
            key = str(r[0]).strip()
            if key.startswith("融資金額"):
                loan_k = _num(r[ti])
            elif len(t.get("data") or []) > 100:      # 逐檔表（彙總表只有 3 列）
                lots[key] = _num(r[ti])
    return lots, loan_k


def compute_market_margin_ratio(date_yyyymmdd: str):
    """當日大盤（上市、含 ETF）融資維持率 %，官方檔任一抓不到回 None。回傳 (ratio, 計入檔數)。"""
    lots, loan_k = _twse_margin_all(date_yyyymmdd)
    closes = _twse_close_map(date_yyyymmdd)
    if not lots or not closes or not loan_k:
        return None
    collat, n = 0.0, 0
    for sym, q in lots.items():
        px = closes.get(sym)
        if q and px:
            collat += q * 1000 * px
            n += 1
    return (collat / (loan_k * 1000) * 100, n) if n else None


def _read_mmr_csv(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            next(f, None)
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 2 and _num(parts[1]) is not None:
                    out[parts[0]] = float(parts[1])
    except OSError:
        pass
    return out


def _append_mmr_live(path: str, date_iso: str, ratio: float) -> None:
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        if new:
            f.write("date,ratio" + chr(10))
        f.write(f"{date_iso},{ratio:.2f}" + chr(10))


def get_market_margin_ratio(as_of_yyyymmdd: str, seed_path: str = None, live_path: str = None):
    """watcher 顯示用：{as_of, ratio, prev, prev_date, pctile, n_hist, n_stock}；當日算不出回 None。

    as_of 傳 `get_chip_bundle` 的 as_of（已 walk-back 到 MI_MARGN 有公布的交易日）。
    prev＝歷史中嚴格早於 as_of 的最後一筆；pctile＝as_of（含）往前 252 筆中 ≤ 當日值的比例。"""
    seed_path = seed_path or MMR_SEED_PATH
    live_path = live_path or MMR_LIVE_PATH
    # 同一交易日只算一次：as_of 已是盤後公布的日子，官方檔不會再變；watcher 每小時刷新籌碼時
    # 不必重抓 MI_MARGN ALL＋MI_INDEX 兩個大檔。失敗（None）不快取，下次刷新再試。
    ck = (as_of_yyyymmdd, seed_path, live_path)
    if ck in _mmr_cache:
        return _mmr_cache[ck]
    res = compute_market_margin_ratio(as_of_yyyymmdd)
    if res is None:
        return None
    ratio, n_stock = res
    d_iso = f"{as_of_yyyymmdd[:4]}-{as_of_yyyymmdd[4:6]}-{as_of_yyyymmdd[6:]}"
    seed = _read_mmr_csv(seed_path)
    live = _read_mmr_csv(live_path)
    if d_iso not in seed and d_iso not in live:
        try:
            _append_mmr_live(live_path, d_iso, ratio)
        except OSError as e:
            print(f"[tw_chip] 維持率本機歷史寫入失敗：{e}")
    hist = {**seed, **live, d_iso: ratio}   # 同日以本次官方原始資料為準
    dates = sorted(k for k in hist if k <= d_iso)
    prev_date = dates[-2] if len(dates) >= 2 else None
    win = [hist[k] for k in dates[-_MMR_PCTILE_WINDOW:]]
    pctile = (sum(1 for v in win if v <= ratio) / len(win) * 100) if len(win) >= _MMR_PCTILE_MIN else None
    out = {"as_of": d_iso, "ratio": ratio, "prev": hist[prev_date] if prev_date else None,
           "prev_date": prev_date, "pctile": pctile, "n_hist": len(win), "n_stock": n_stock}
    _mmr_cache[ck] = out
    return out
