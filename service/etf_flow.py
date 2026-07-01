"""
service/etf_flow.py
美國現貨比特幣 ETF 每日淨流量 — 真實值（Farside Investors）

來源：farside.co.uk
  - 全歷史頁：/bitcoin-etf-flow-all-data/（自 2024-01-11 起，含每日各檔 + Total）
  - 近期頁：  /btc/（較小，當備援）
  表格用 pandas.read_html 解析；負值以括號表示 "(396.6)"、含千分位逗號；單位百萬美元。

⚠️ 脆弱性（已知陷阱）：
  - Farside 對 datacenter IP（Streamlit Cloud / GitHub Actions）可能回 403。
    本機（公司網路）實測可抓。策略：抓得到就更新 db/etf_flow.json 快取，
    抓不到回退已落地快取（沿用 bottom_metrics / btc db「雲端讀 repo 內 db」pattern）。
  - 表格結構若改版，read_html 仍會回多個表，靠「含 Total 欄且列數多」挑正確表。

用途：core/relative_high「鏈上派發」維度 — 連續淨流出天數 + 近 5 日累計。
純資料層，無 Streamlit 依賴。
"""
import os
import io
import re
import json
import time
import logging
from datetime import datetime, timezone

import urllib3
import pandas as pd

from config import SSL_VERIFY
from core.http_client import safe_get

logger = logging.getLogger(__name__)

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CACHE = os.path.join(os.path.dirname(__file__), "..", "db", "etf_flow.json")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
_URL_ALL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
_URL_RECENT = "https://farside.co.uk/btc/"
_CACHE_MAX_AGE_H = 12


def _load() -> dict:
    try:
        with open(_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[etf_flow] 快取寫入失敗：{e}")


def _parse_flow_value(x) -> float:
    """'(396.6)' -> -396.6；'1,234.5' -> 1234.5；'-' / '' -> None。"""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "nan", "NaN"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _parse_html_to_daily(html: str) -> dict:
    """從 Farside HTML 解析 {date_str(YYYY-MM-DD): total_flow_musd}。"""
    tables = pd.read_html(io.StringIO(html))
    out = {}
    for t in tables:
        cols = [str(c) for c in t.columns]
        if "Date" not in cols or "Total" not in cols or len(t) < 5:
            continue
        for _, row in t.iterrows():
            d_raw = str(row["Date"]).strip()
            if d_raw.lower() in ("total", "nan", ""):
                continue
            try:
                d = datetime.strptime(d_raw, "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
            val = _parse_flow_value(row["Total"])
            # 跳過佔位 0.0：Farside 對「最新未定案日」常回 0（實測 06-30 夾在一串流出中為 0.0）。
            # 交易日淨流量恰為 0.0 極罕見，且 0 流量本就無方向訊號 → 不寫入快取、不打斷 streak，
            # 待該日真實值出來再由後續 fetch merge 覆蓋補上。
            if val is not None and val != 0.0:
                out[d] = val
        if out:
            break
    if not out:
        raise ValueError("Farside 表格解析無有效資料")
    return out


def fetch_etf_flow(force: bool = False) -> dict:
    """
    回傳 {date_str: total_net_flow_musd}（百萬美元，負=淨流出）。
    新鮮快取（< 12h）直接用；抓取失敗回退快取。
    """
    cache = _load()
    fresh = (not force and cache.get("_ts")
             and (time.time() - cache["_ts"]) < _CACHE_MAX_AGE_H * 3600
             and cache.get("data"))
    if fresh:
        return cache["data"]

    for url in (_URL_ALL, _URL_RECENT):
        try:
            r = safe_get(url, headers=_UA, timeout=25, verify=SSL_VERIFY)
            data = _parse_html_to_daily(r.text)
            merged = dict(cache.get("data") or {})
            merged.update(data)   # 新資料覆蓋舊，保留歷史
            _save({"_ts": time.time(), "data": merged})
            logger.info(f"[etf_flow] 更新成功（{url}）：{len(data)} 筆，累計 {len(merged)} 筆")
            return merged
        except Exception as e:
            logger.warning(f"[etf_flow] 抓取失敗（{url}）：{type(e).__name__}: {e}")

    logger.warning("[etf_flow] 所有來源失敗，回退快取")
    return cache.get("data", {})


def _summarize(data: dict) -> dict:
    """從 {date: total_flow} 算摘要（純函數，可測）。

    佔位 0.0 一律視為當日無資料（過濾）：Farside 對最新未定案日常回 0，若計入會
    (a) 把 latest 誤標中性、(b) 打斷連續流出/流入 streak（假 0 夾在流出串中會使
    consecutive_outflow_days 歸 0，遮蔽真實派發訊號）。交易日淨流量恰為 0.0 極罕見，
    且 0 流量本無方向訊號，過濾後 streak 正確跨越該日。
    """
    out = {"latest": None, "latest_date": None, "consecutive_outflow_days": 0,
           "consecutive_inflow_days": 0, "cum_5d": None, "n": 0, "asof": None,
           "stale_days": None}
    items = sorted((d, v) for d, v in data.items() if v != 0.0)   # 過濾佔位 0.0
    out["n"] = len(items)
    if not items:
        return out
    dates = [d for d, _ in items]
    vals  = [v for _, v in items]
    out["latest"] = vals[-1]
    out["latest_date"] = out["asof"] = dates[-1]
    # 最新一筆距今天數（ETF 僅交易日更新：週末 1-3 天屬正常，>4 天代表來源久未更新）
    try:
        out["stale_days"] = (datetime.now(timezone.utc).date()
                             - datetime.strptime(dates[-1], "%Y-%m-%d").date()).days
    except ValueError:
        pass
    out["cum_5d"] = sum(vals[-5:])
    out_cnt = in_cnt = 0
    for v in reversed(vals):
        if v < 0:
            out_cnt += 1
        else:
            break
    for v in reversed(vals):
        if v > 0:
            in_cnt += 1
        else:
            break
    out["consecutive_outflow_days"] = out_cnt
    out["consecutive_inflow_days"] = in_cnt
    return out


def get_etf_flow_summary(force: bool = False) -> dict:
    """
    回傳 ETF 流量摘要：
      {latest, latest_date, consecutive_outflow_days, consecutive_inflow_days,
       cum_5d, n, asof, stale_days}
    - consecutive_outflow_days：自最新日往回，連續淨流出（<0）天數（逃頂派發訊號）
    - consecutive_inflow_days：自最新日往回，連續淨流入（>0）天數（抄底吸籌訊號）
    - cum_5d：近 5 個（非零）交易日累計淨流量（百萬美元）
    - n：可用資料筆數（已排除佔位 0.0）；無資料時各值為 None
    佔位 0.0 的處理見 `_summarize`。
    """
    return _summarize(fetch_etf_flow(force=force))
