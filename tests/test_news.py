"""
tests/test_news.py
新聞模組單元測試 — 全部以 monkeypatch 隔離外部 API，不打真 Gemini/HTTP。

涵蓋：
  - humanize_age 相對時間邊界
  - _norm_title / _clean_html 純函式
  - _aggregate 跨來源去重 + 時間排序
  - _parse_json_array 容忍 markdown 包裹與壞輸入
  - fetch_crypto_news 全來源失敗 → 靜態 fallback
  - enrich_news_zh 停用/快取命中時不打 API
"""
from datetime import datetime, timezone, timedelta

import pytest


# ── 純函式 ────────────────────────────────────────────────────────────────
def test_humanize_age():
    from service.news import humanize_age
    now = datetime.now(timezone.utc)
    assert humanize_age(None) == ""
    assert "分鐘前" in humanize_age(now - timedelta(minutes=5))
    assert "小時前" in humanize_age(now - timedelta(hours=3))
    assert "天前" in humanize_age(now - timedelta(days=2))
    # naive datetime 應被視為 UTC，不報錯
    assert humanize_age(datetime(2020, 1, 1)) != ""


def test_norm_title():
    from service.news import _norm_title
    assert _norm_title("Bitcoin Hits $70,000!") == _norm_title("bitcoin hits 70000")
    assert _norm_title("") == ""


def test_clean_html():
    from service.news import _clean_html
    assert _clean_html("<p>Hello &amp; <b>World</b></p>") == "Hello World"
    assert _clean_html(None) == ""
    assert _clean_html("") == ""


# ── 聚合去重 ──────────────────────────────────────────────────────────────
def test_aggregate_dedup_and_sort():
    from service.news import NewsItem, _aggregate
    a = NewsItem("Bitcoin hits 70k", "u1", "CoinDesk",
                 published_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    b = NewsItem("bitcoin hits 70k!!", "u2", "Decrypt",   # 與 a 正規化後同題
                 published_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    c = NewsItem("Ethereum upgrade ships", "u3", "CoinDesk",
                 published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    out = _aggregate([[a], [b, c]], 10)
    urls = [i.url for i in out]
    assert "u1" in urls and "u3" in urls   # a 先出現被保留
    assert "u2" not in urls                 # b 視為重複被剔除
    assert out[0].url == "u1"               # a(1/2) 比 c(1/1) 新，排前


def test_aggregate_respects_limit():
    from service.news import NewsItem, _aggregate
    items = [NewsItem(f"title {i}", f"u{i}", "S",
                      published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
             for i in range(20)]
    assert len(_aggregate([items], 8)) == 8


# ── Gemini 輸出解析 ──────────────────────────────────────────────────────
def test_parse_json_array():
    from service.news_i18n import _parse_json_array
    assert _parse_json_array('[{"id":0}]') == [{"id": 0}]
    assert _parse_json_array('```json\n[{"id":1}]\n```') == [{"id": 1}]
    assert _parse_json_array("前言... [{\"id\":2}] 後綴") == [{"id": 2}]
    assert _parse_json_array("not json") == []
    assert _parse_json_array("") == []


# ── fetch_crypto_news fallback ───────────────────────────────────────────
def test_fetch_all_sources_fail_returns_fallback(monkeypatch):
    from service import news

    def _boom():
        raise RuntimeError("source down")

    monkeypatch.setattr(news, "_SOURCES", [_boom, _boom])
    monkeypatch.setattr(news, "fetch_trending_coins", lambda *a, **k: [])
    feed = news.fetch_crypto_news.__wrapped__(8)
    assert feed.is_fallback is True
    assert len(feed.items) > 0          # 靜態備援不開天窗


# ── enrich_news_zh 不打 API 的兩條路徑 ───────────────────────────────────
class _Item:
    def __init__(self, url):
        self.url = url
        self.title = "t"
        self.raw_summary = ""
        self.title_zh = None
        self.summary_zh = None
        self.sentiment = None


def test_enrich_no_key_is_noop(monkeypatch):
    from service import news_i18n
    from core import gemini_client
    monkeypatch.setattr(gemini_client, "is_available", lambda: False)
    it = _Item("u")
    news_i18n.enrich_news_zh([it])
    assert it.title_zh is None          # 無金鑰：保持英文 fallback


def test_enrich_cache_hit_skips_api(monkeypatch):
    from service import news_i18n
    from core import gemini_client
    monkeypatch.setattr(news_i18n, "NEWS_I18N_ENABLED", True)
    monkeypatch.setattr(gemini_client, "is_available", lambda: True)
    monkeypatch.setattr(news_i18n, "_load_cache",
                        lambda: {"u1": {"title_zh": "中文標題",
                                        "summary_zh": "小結", "sentiment": "bull"}})
    calls = {"n": 0}
    monkeypatch.setattr(news_i18n, "_translate_batch",
                        lambda batch: calls.__setitem__("n", calls["n"] + 1))
    it = _Item("u1")
    news_i18n.enrich_news_zh([it])
    assert it.title_zh == "中文標題"
    assert it.sentiment == "bull"
    assert calls["n"] == 0              # 快取命中 → 不打 Gemini
