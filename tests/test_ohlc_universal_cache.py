"""ohlc_universal 批次2 效率測試（W-3 resolved symbol 快取 / W-6 Session 單例）。不打網路。"""
import sys

import pytest
import requests

sys.path.insert(0, ".")
import service.ohlc_universal as ohlc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    monkeypatch.setattr(ohlc, "_RESOLVED", {})
    monkeypatch.setattr(ohlc, "_SESSION", None)


# ── W-6：Session 模組級單例 ──────────────────────────────────────────────────

def test_session_is_singleton():
    assert ohlc._session() is ohlc._session()
    assert ohlc._session().verify is False


# ── W-3：候選清單與解析快取 ──────────────────────────────────────────────────

def test_candidates_unresolved_tw_tries_two():
    assert ohlc._tw_candidates("6509.TW") == ["6509.TW", "6509.TWO"]
    assert ohlc._tw_candidates("QQQ") == ["QQQ"]


def test_candidates_resolved_returns_single():
    ohlc._RESOLVED["6509.TW"] = "6509.TWO"
    assert ohlc._tw_candidates("6509.TW") == ["6509.TWO"]


class _FakeResp:
    def __init__(self, ok, meta=None):
        self._ok, self._meta = ok, meta

    def raise_for_status(self):
        if not self._ok:
            raise requests.HTTPError("404")

    def json(self):
        return {"chart": {"result": [{"meta": self._meta}]}}


class _FakeSession:
    """上櫃股情境：.TW 一律 404、.TWO 有報價。記錄打過的 symbol 順序。"""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        sym = url.rsplit("/", 1)[-1]
        self.calls.append(sym)
        if sym.endswith(".TWO"):
            return _FakeResp(True, {"regularMarketPrice": 123.5, "regularMarketTime": 1,
                                    "previousClose": 120.0, "regularMarketVolume": 1000})
        return _FakeResp(False)


def test_live_quote_caches_resolution_no_more_404(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr(ohlc, "_session", lambda: fake)

    q1 = ohlc.fetch_live_quote("6509.TW")
    assert q1["price"] == 123.5
    assert fake.calls == ["6509.TW", "6509.TWO"]    # 首次：.TW 404 後試 .TWO

    fake.calls.clear()
    q2 = ohlc.fetch_live_quote("6509.TW")
    assert q2["price"] == 123.5
    assert fake.calls == ["6509.TWO"]               # 之後：直打 .TWO，不再吃 404
