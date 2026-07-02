"""service.tw_chip.get_shares_outstanding 單元測試（TWSE/TPEx OpenAPI，requests 全部 mock，零網路）。"""
import time

import service.tw_chip as tw_chip


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _reset_cache():
    tw_chip._shares_cache.clear()


def test_twse_hit_returns_shares(monkeypatch):
    _reset_cache()
    payload = [{"公司代號": "2330", "已發行普通股數或TDR原股發行股數": "25932370067",
                "實收資本額": "259323700670", "普通股每股面額": "新台幣10.0000元"}]

    def fake_get(self, url, **kw):
        assert url == tw_chip._TWSE_OPEN_T187
        return _FakeResp(payload)

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert tw_chip.get_shares_outstanding("2330") == 25932370067.0


def test_tpex_fallback_when_not_in_twse(monkeypatch):
    _reset_cache()

    def fake_get(self, url, **kw):
        if url == tw_chip._TWSE_OPEN_T187:
            return _FakeResp([{"公司代號": "2330", "已發行普通股數或TDR原股發行股數": "25932370067"}])
        if url == tw_chip._TPEX_OPEN_T187:
            return _FakeResp([{"SecuritiesCompanyCode": "6488", "IssueShares": "44232373"}])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert tw_chip.get_shares_outstanding("6488") == 44232373.0
    assert tw_chip.get_shares_outstanding("9999") is None   # 兩邊都查無


def test_cache_avoids_refetch_within_ttl(monkeypatch):
    _reset_cache()
    calls = {"n": 0}

    def fake_get(self, url, **kw):
        calls["n"] += 1
        return _FakeResp([{"公司代號": "2330", "已發行普通股數或TDR原股發行股數": "25932370067"}])

    monkeypatch.setattr("requests.Session.get", fake_get)
    tw_chip.get_shares_outstanding("2330")
    tw_chip.get_shares_outstanding("2330")
    assert calls["n"] == 1   # 第二次命中快取，不重抓


def test_fetch_failure_falls_back_to_stale_cache(monkeypatch):
    _reset_cache()
    good = [{"公司代號": "2330", "已發行普通股數或TDR原股發行股數": "25932370067"}]
    state = {"n": 0}

    def fake_get(self, url, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return _FakeResp(good)
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert tw_chip.get_shares_outstanding("2330") == 25932370067.0   # 第一次成功、寫入快取
    tw_chip._shares_cache[tw_chip._TWSE_OPEN_T187] = (0.0, tw_chip._shares_cache[tw_chip._TWSE_OPEN_T187][1])
    assert tw_chip.get_shares_outstanding("2330") == 25932370067.0   # TTL 過期後重抓失敗 → 退回舊快取


def test_fetch_failure_no_prior_cache_returns_empty(monkeypatch):
    _reset_cache()

    def fake_get(self, url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert tw_chip.get_shares_outstanding("2330") is None
