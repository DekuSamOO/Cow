"""
service/news.py
加密貨幣社群熱門新聞服務 — 多來源聚合

來源（兩類，盡量各抓數則後聚合去重，非單純 fallback 鏈）：
  媒體 (media)：
    - CryptoCompare News API（免費無金鑰，含 tags/categories）
    - Cointelegraph RSS
    - CoinDesk RSS
    - Decrypt RSS
  社群熱度 (trending)：
    - CoinGecko /search/trending（24h 熱搜幣種，免金鑰）
      取代 Reddit hot.json（公司網路/雲端共享 IP 均被 403 封鎖，需 OAuth）
  X/Twitter：免費 API 已關閉，無穩定免費抓法 → 暫不納入

聚合策略：
  - 每個來源各抓數則，全部成功的合併、跨來源以標題正規化去重
  - 依發布時間新→舊排序，取前 limit 則
  - 全部來源都失敗才回靜態 fallback（不讓版面開天窗）

慣例對齊：
  - 走 core.http_client.safe_get(verify=SSL_VERIFY)，本地關 SSL 驗證繞過企業 Proxy
  - 掛 @st.cache_data(ttl=14400)（4 小時）— 配合 Gemini 中文化省 token，且不接 fragment
    （CLAUDE.md 陷阱 #1：list 傳入 @st.fragment 會序列化失敗導致靜默失效）
  - 回傳 NewsFeed 帶來源摘要，供 UI 顯示（service 來源追蹤慣例 #6）
"""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import streamlit as st
import urllib3

from config import SSL_VERIFY
from core.http_client import safe_get
from service.news_i18n import enrich_news_zh

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# 每個來源抓取則數（聚合去重後再取 limit）
_PER_SOURCE = 8


@dataclass
class NewsItem:
    title: str
    url: str
    source: str                                # 媒體/版面名稱（如 CoinDesk、r/Bitcoin）
    source_type: str = "media"                 # "media" | "forum"
    published_at: Optional[datetime] = None    # UTC
    tags: List[str] = field(default_factory=list)
    image_url: Optional[str] = None
    score: Optional[int] = None                # 論壇熱度（Reddit ups），媒體為 None
    raw_summary: str = ""                       # 英文原摘要（供 Gemini 產生中文小結）
    # 中文化欄位（由 task #3 的 Gemini 階段填入）
    title_zh: Optional[str] = None
    summary_zh: Optional[str] = None
    sentiment: Optional[str] = None            # "bull" | "bear" | "neutral"


@dataclass
class NewsFeed:
    items: List[NewsItem] = field(default_factory=list)
    source: str = "—"                          # 來源摘要（供 UI 顯示）
    is_fallback: bool = False
    trending: List[str] = field(default_factory=list)  # CoinGecko 24h 社群熱搜幣種


# ──────────────────────────────────────────────────────────────────────────
# 共用 RSS 解析
# ──────────────────────────────────────────────────────────────────────────
def _clean_html(text: Optional[str]) -> str:
    """去除 HTML 標籤與實體，壓縮空白（RSS description 常含 HTML）。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[#a-zA-Z0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_rss(content: bytes, source_name: str, limit: int) -> List[NewsItem]:
    root = ET.fromstring(content)
    items: List[NewsItem] = []
    for node in list(root.iter("item"))[:limit]:
        title_el = node.find("title")
        link_el = node.find("link")
        date_el = node.find("pubDate")
        desc_el = node.find("description")
        published = None
        if date_el is not None and date_el.text:
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
                try:
                    published = datetime.strptime(date_el.text.strip(), fmt)
                    break
                except ValueError:
                    continue
        title = (title_el.text or "").strip() if title_el is not None else ""
        url = (link_el.text or "").strip() if link_el is not None else ""
        raw_summary = _clean_html(desc_el.text if desc_el is not None else "")[:300]
        if title and url:
            items.append(NewsItem(
                title=title, url=url, source=source_name,
                source_type="media", published_at=published,
                raw_summary=raw_summary,
            ))
    return items


# ──────────────────────────────────────────────────────────────────────────
# 比特幣 / 泛加密 過濾（使用者只要 BTC 或加密貨幣大盤新聞，不要山寨幣個別新聞）
# ──────────────────────────────────────────────────────────────────────────
# 比特幣關鍵字（含則保留，前提是未提任何山寨幣）
_BTC_TERMS = ("bitcoin", "btc", "satoshi", "₿", "比特幣", "中本聰")
# 泛加密/總經關鍵字（與大盤相關、非單一山寨幣 → 保留）
_GENERAL_CRYPTO_TERMS = (
    "crypto", "cryptocurrenc", "digital asset", "blockchain", "stablecoin",
    "regulat", "securities", "u.s. sec", "etf", "federal reserve", " fed ", "macro",
    "interest rate", "treasury", "spot etf", "加密", "虛擬貨幣", "穩定幣", "監管",
    "升息", "降息", "聯準會", "比特幣現貨", "數位資產", "區塊鏈",
)
# 山寨幣關鍵字（**只要提到就剔除**，即使同時提 BTC）——使用者只要純比特幣/泛加密大盤新聞。
# 注意：Bitcoin Cash / BCH 為山寨幣，須先歸類於此，且其 "bitcoin" 字串不得誤判為比特幣。
_ALTCOIN_TERMS = (
    "bitcoin cash", "bch", "ethereum", "ether ", "solana", "xrp", "ripple",
    "cardano", "dogecoin", "shiba", "bnb", "binance coin", "tron", "polkadot",
    "avalanche", "avax", "litecoin", "chainlink", "polygon", "toncoin", "pepe",
    "aptos", "sui ", "near protocol", "uniswap", "比特幣現金", "以太坊", "以太幣",
    "瑞波", "狗狗幣", "萊特幣", "索拉納",
)


def _is_btc_crypto(item: "NewsItem") -> bool:
    """嚴格過濾：只要提到任一山寨幣即剔除（含 Bitcoin Cash）；其餘保留 BTC / 泛加密大盤新聞。"""
    text = f"{item.title} {item.raw_summary} {' '.join(item.tags)}".lower()
    if any(t in text for t in _ALTCOIN_TERMS):
        return False            # 提到山寨幣 → 一律剔除（即使也提 BTC）
    # 把 "bitcoin cash" 已在上面剔除；此處 bitcoin 必為真比特幣
    if any(t in text for t in _BTC_TERMS):
        return True
    return any(t in text for t in _GENERAL_CRYPTO_TERMS)


# ──────────────────────────────────────────────────────────────────────────
# 各來源 fetcher
# ──────────────────────────────────────────────────────────────────────────
def _fetch_cryptocompare(limit: int) -> List[NewsItem]:
    r = safe_get(
        "https://min-api.cryptocompare.com/data/v2/news/?lang=EN",
        timeout=8, verify=SSL_VERIFY, headers=_HEADERS,
    )
    raw = (r.json().get("Data") or [])[:limit]
    items: List[NewsItem] = []
    for a in raw:
        ts = a.get("published_on")
        published = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        tags = [t.strip() for t in (a.get("tags") or "").split("|") if t.strip()][:3]
        items.append(NewsItem(
            title=(a.get("title") or "").strip(),
            url=a.get("url", ""),
            source=a.get("source_info", {}).get("name") or a.get("source", "CryptoCompare"),
            source_type="media",
            published_at=published,
            tags=tags,
            image_url=a.get("imageurl") or None,
            raw_summary=_clean_html(a.get("body"))[:300],
        ))
    return items


def _fetch_cointelegraph(limit: int) -> List[NewsItem]:
    r = safe_get("https://cointelegraph.com/rss", timeout=6, verify=SSL_VERIFY, headers=_HEADERS)
    return _parse_rss(r.content, "Cointelegraph", limit)


def _fetch_coindesk(limit: int) -> List[NewsItem]:
    r = safe_get(
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        timeout=6, verify=SSL_VERIFY, headers=_HEADERS,
    )
    return _parse_rss(r.content, "CoinDesk", limit)


def _fetch_decrypt(limit: int) -> List[NewsItem]:
    r = safe_get("https://decrypt.co/feed", timeout=6, verify=SSL_VERIFY, headers=_HEADERS)
    return _parse_rss(r.content, "Decrypt", limit)


def fetch_trending_coins(limit: int = 7) -> List[str]:
    """CoinGecko 24h 社群熱搜幣種（反映散戶關注度）。失敗回空 list。
    Reddit 免認證在公司網路/雲端共享 IP 均被 403 封鎖，改用此免金鑰熱度指標。
    """
    try:
        r = safe_get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=6, verify=SSL_VERIFY, headers=_HEADERS,
        )
        coins = r.json().get("coins", [])
        out: List[str] = []
        for c in coins[:limit]:
            sym = (c.get("item", {}).get("symbol") or "").upper().strip()
            if sym:
                out.append(sym)
        return out
    except Exception:
        return []


_SOURCES = [
    lambda n=_PER_SOURCE: _fetch_cryptocompare(n),
    lambda n=_PER_SOURCE: _fetch_cointelegraph(n),
    lambda n=_PER_SOURCE: _fetch_coindesk(n),
    lambda n=_PER_SOURCE: _fetch_decrypt(n),
]


# ──────────────────────────────────────────────────────────────────────────
# 去重與聚合
# ──────────────────────────────────────────────────────────────────────────
def _norm_title(title: str) -> str:
    """標題正規化作去重 key：小寫、去非英數、取前 50 字元。"""
    return re.sub(r"[^a-z0-9]", "", title.lower())[:50]


def _aggregate(buckets: List[List[NewsItem]], limit: int) -> List[NewsItem]:
    seen = set()
    merged: List[NewsItem] = []
    for bucket in buckets:
        for it in bucket:
            key = _norm_title(it.title)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(it)
    # 依發布時間新→舊排序；無時間者排最後
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _sort_key(it: NewsItem):
        dt = it.published_at
        if dt is None:
            return _epoch
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    merged.sort(key=_sort_key, reverse=True)
    return merged[:limit]


_STATIC_FALLBACK = [
    NewsItem("CoinDesk — 加密貨幣即時新聞首頁", "https://www.coindesk.com/", "CoinDesk"),
    NewsItem("Cointelegraph — Bitcoin News", "https://cointelegraph.com/tags/bitcoin", "Cointelegraph"),
    NewsItem("Decrypt — 加密貨幣新聞", "https://decrypt.co/news", "Decrypt"),
]


@st.cache_data(ttl=14400)
def fetch_crypto_news(limit: int = 8) -> NewsFeed:
    """多來源聚合加密貨幣熱門新聞，4 小時快取。永不丟例外給 UI。"""
    buckets: List[List[NewsItem]] = []
    for fetch in _SOURCES:
        try:
            # 只保留 BTC / 泛加密大盤新聞，剔除山寨幣個別新聞
            got = [it for it in fetch() if it.title and it.url and _is_btc_crypto(it)]
            if got:
                buckets.append(got)
        except Exception:
            continue

    trending = fetch_trending_coins()

    if not buckets:
        return NewsFeed(
            items=list(_STATIC_FALLBACK), source="靜態備援",
            is_fallback=True, trending=trending,
        )

    items = _aggregate(buckets, limit)
    enrich_news_zh(items)   # Gemini 中文化（含持久化快取，翻過的不重打）
    return NewsFeed(
        items=items,
        source=f"{len(buckets)} 個來源聚合",
        is_fallback=False,
        trending=trending,
    )


# ──────────────────────────────────────────────────────────────────────────
# 情緒彙總（UI 速覽與每日推播共用，避免兩處重複 net/mood 門檻邏輯）
# ──────────────────────────────────────────────────────────────────────────
SENTIMENT_EMOJI = {"bull": "🟢", "bear": "🔴", "neutral": "⚪"}


@dataclass
class SentimentSummary:
    bull: int = 0
    bear: int = 0
    neutral: int = 0
    mood: Optional[str] = None   # "🟢 輿情偏多" / "🔴 輿情偏空" / "⚪ 輿情中性"；無資料則 None

    @property
    def has_data(self) -> bool:
        return bool(self.bull or self.bear or self.neutral)


def summarize_sentiment(items: List[NewsItem]) -> SentimentSummary:
    """彙總一批新聞的整體輿情：多空中性計數 + 偏多/偏空門檻（net ±2）判定。"""
    bull = sum(1 for it in items if it.sentiment == "bull")
    bear = sum(1 for it in items if it.sentiment == "bear")
    neu  = sum(1 for it in items if it.sentiment == "neutral")
    mood = None
    if bull or bear or neu:
        net = bull - bear
        mood = "🟢 輿情偏多" if net >= 2 else ("🔴 輿情偏空" if net <= -2 else "⚪ 輿情中性")
    return SentimentSummary(bull=bull, bear=bear, neutral=neu, mood=mood)


def humanize_age(dt: Optional[datetime]) -> str:
    """回傳相對時間字串（X 分鐘前 / X 小時前 / X 天前）。"""
    if dt is None:
        return ""
    now = datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "剛剛"
    if secs < 3600:
        return f"{int(secs // 60)} 分鐘前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小時前"
    return f"{int(secs // 86400)} 天前"
