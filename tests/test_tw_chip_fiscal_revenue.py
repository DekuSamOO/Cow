"""service.tw_chip 的財報基期與月營收（2026-08-12 那批新增，requests 全部 mock，零網路）。

只測「壞掉不會噴錯、只會靜默回 None 或給錯數字」的行為：民國轉換拒絕解析不了的輸入、
西元格式不可被誤判成民國、舊欄數檔案不可讓整組估值失效、月營收上市→上櫃 fallback、
抓取失敗退回舊快取而非清空、成長率照抄來源而非由金額回推。
"""
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


def _table(rows):
    """TWSE/TPEx 單日檔的回應外殼（`_fetch_market_file` 會深找 fields+data）。"""
    return {"stat": "OK", "fields": [], "data": rows}


def _reset():
    tw_chip._cache.clear()
    tw_chip._rev_cache.clear()


# ── 民國轉換三支：解析不出來一律 None，不猜 ─────────────────────────────────

def test_roc_quarter_accepts_both_twse_and_tpex_formats():
    """TWSE 給 `115/2`、TPEx 給 `115Q1`，同一支要都吃得下（否則上櫃股會整批沒基期）。"""
    assert tw_chip._roc_quarter("115/2") == "2026Q2"
    assert tw_chip._roc_quarter("115Q1") == "2026Q1"
    assert tw_chip._roc_quarter("115q4") == "2026Q4"
    assert tw_chip._roc_quarter(" 115/3 ") == "2026Q3"      # 前後空白
    assert tw_chip._roc_quarter("99/4") == "2010Q4"         # 兩位數民國年


def test_roc_quarter_rejects_ad_format_and_garbage():
    """西元 `2026Q2` 若被當成民國會變 3937Q2——無聲的百年誤差，這是最該擋住的一種錯。"""
    assert tw_chip._roc_quarter("2026Q2") is None
    assert tw_chip._roc_quarter("115/5") is None            # 季別只有 1-4
    assert tw_chip._roc_quarter("115") is None
    assert tw_chip._roc_quarter("") is None
    assert tw_chip._roc_quarter(None) is None


def test_roc_ym_converts_and_rejects():
    assert tw_chip._roc_ym("11507") == "2026-07"
    assert tw_chip._roc_ym("09912") == "2010-12"
    assert tw_chip._roc_ym(" 11501 ") == "2026-01"
    assert tw_chip._roc_ym("115") is None                   # 長度不足
    assert tw_chip._roc_ym("2026-07") is None               # 已是西元格式
    assert tw_chip._roc_ym(None) is None


def test_roc_ymd_converts_and_rejects():
    assert tw_chip._roc_ymd("1150811") == "2026-08-11"
    assert tw_chip._roc_ymd("1150812") == "2026-08-12"
    assert tw_chip._roc_ymd("990811") == "2010-08-11"
    assert tw_chip._roc_ymd("11508") is None                # 長度不足
    assert tw_chip._roc_ymd("2026-08-11") is None
    assert tw_chip._roc_ymd(None) is None


# ── get_valuation 的財報基期欄 ──────────────────────────────────────────────

def test_valuation_twse_returns_fiscal_quarter(monkeypatch):
    """BWIBBU 欄 7 是 PE 的近四季 EPS 截止季，raw 一併保留供人工核對。"""
    _reset()
    row = ["6782", "視陽", "207.50", "4.05", "114", "12.58", "2.94", "115/2"]
    monkeypatch.setattr("requests.Session.get",
                        lambda self, url, **kw: _FakeResp(_table([row])))
    v = tw_chip.get_valuation("6782", "20260812")
    assert v["pe"] == 12.58 and v["pb"] == 2.94 and v["close"] == 207.50
    assert v["pe_fiscal_quarter"] == "2026Q2"
    assert v["pe_fiscal_quarter_raw"] == "115/2"


def test_valuation_survives_legacy_seven_column_row(monkeypatch):
    """舊檔只有 7 欄時，PE/PB/殖利率仍要照常回——加值欄缺漏不該讓整組估值失效。"""
    _reset()
    row = ["6782", "視陽", "207.50", "4.05", "114", "12.58", "2.94"]
    monkeypatch.setattr("requests.Session.get",
                        lambda self, url, **kw: _FakeResp(_table([row])))
    v = tw_chip.get_valuation("6782", "20260812")
    assert v["pe"] == 12.58 and v["pb"] == 2.94
    assert v["pe_fiscal_quarter"] is None
    assert v["pe_fiscal_quarter_raw"] is None


def test_valuation_falls_back_to_tpex_with_q_format(monkeypatch):
    """上市查無 → 轉 TPEx。TPEx 欄序不同（PE 在 idx 2）且季別寫成 `115Q1`。"""
    _reset()
    tpex_row = ["6488", "環球晶", "32.47", "8.40", "114", "0.91", "10.63", "115Q1"]

    def fake_get(self, url, **kw):
        if "BWIBBU_d" in url:
            return _FakeResp(_table([]))            # 上市查無
        return _FakeResp(_table([tpex_row]))

    monkeypatch.setattr("requests.Session.get", fake_get)
    v = tw_chip.get_valuation("6488", "20260812")
    assert v["pe"] == 32.47 and v["pb"] == 10.63
    assert v["close"] is None                       # peQryDate 無收盤價欄
    assert v["pe_fiscal_quarter"] == "2026Q1"


# ── get_monthly_revenue ────────────────────────────────────────────────────

_REV_ROW = {
    "公司代號": "6782", "公司名稱": "視陽", "產業別": "生技醫療業",
    "資料年月": "11507", "出表日期": "1150812",
    "營業收入-當月營收": "465679", "營業收入-上月營收": "450881",
    "營業收入-去年當月營收": "397335",
    # 刻意與金額回推值（465679/450881-1＝3.28%）不同，用來證明是照抄來源欄、不是自己算
    "營業收入-上月比較增減(%)": "9.99",
    "營業收入-去年同月增減(%)": "17.20",
    "累計營業收入-當月累計營收": "2961473",
    "累計營業收入-去年累計營收": "2419963",
    "累計營業收入-前期比較增減(%)": "22.37",
    "備註": "-",
}


def test_monthly_revenue_maps_keys_units_and_limitation(monkeypatch):
    _reset()
    monkeypatch.setattr("requests.Session.get",
                        lambda self, url, **kw: _FakeResp([_REV_ROW]))
    r = tw_chip.get_monthly_revenue("6782")
    assert r["source"] == "TWSE"
    assert r["data_month"] == "2026-07" and r["data_month_raw"] == "11507"
    assert r["published_at"] == "2026-08-12" and r["published_at_raw"] == "1150812"
    assert r["revenue_ktwd"] == 465679.0            # 仟元，單位寫進 key 名
    assert r["cum_revenue_ktwd"] == 2961473.0
    assert r["industry"] == "生技醫療業"
    assert r["note"] is None                        # 來源的 "-" 視同無備註
    assert "不可回測" in r["limitation"]


def test_monthly_revenue_copies_growth_rate_instead_of_recomputing(monkeypatch):
    """成長率照抄來源官方 (%) 欄。自己由三個金額回推會得到 3.28，與來源口徑不同。"""
    _reset()
    monkeypatch.setattr("requests.Session.get",
                        lambda self, url, **kw: _FakeResp([_REV_ROW]))
    r = tw_chip.get_monthly_revenue("6782")
    assert r["mom_pct"] == 9.99                     # 照抄，不是 3.28
    assert r["yoy_pct"] == 17.20
    assert r["cum_yoy_pct"] == 22.37


def test_monthly_revenue_falls_back_to_tpex(monkeypatch):
    """兩端 JSON key 完全相同 → 共用一套解析；上市查無才轉上櫃。"""
    _reset()
    tpex_row = dict(_REV_ROW, **{"公司代號": "6488", "公司名稱": "環球晶"})

    def fake_get(self, url, **kw):
        if url == tw_chip._TWSE_OPEN_REV:
            return _FakeResp([_REV_ROW])
        if url == tw_chip._TPEX_OPEN_REV:
            return _FakeResp([tpex_row])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("requests.Session.get", fake_get)
    r = tw_chip.get_monthly_revenue("6488")
    assert r["source"] == "TPEx" and r["company_name"] == "環球晶"
    assert tw_chip.get_monthly_revenue("9999") is None   # 兩邊都查無


def test_monthly_revenue_forces_utf8_encoding(monkeypatch):
    """該 domain 的 requests 編碼偵測會猜錯 → 必須強制 utf-8，否則中文欄位全變亂碼。"""
    _reset()
    resp = _FakeResp([_REV_ROW])
    monkeypatch.setattr("requests.Session.get", lambda self, url, **kw: resp)
    tw_chip.get_monthly_revenue("6782")
    assert resp.encoding == "utf-8"


def test_monthly_revenue_fetch_failure_falls_back_to_stale_cache(monkeypatch):
    """月營收一個月才變一次，抓取失敗時舊快取仍可信——退回舊值而非清空。"""
    _reset()
    state = {"n": 0}

    def fake_get(self, url, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return _FakeResp([_REV_ROW])
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.Session.get", fake_get)
    assert tw_chip.get_monthly_revenue("6782")["revenue_ktwd"] == 465679.0
    # 手動讓 TTL 過期 → 重抓失敗
    tw_chip._rev_cache[tw_chip._TWSE_OPEN_REV] = (0.0, tw_chip._rev_cache[tw_chip._TWSE_OPEN_REV][1])
    assert tw_chip.get_monthly_revenue("6782")["revenue_ktwd"] == 465679.0


def test_monthly_revenue_fetch_failure_without_cache_returns_none(monkeypatch):
    _reset()

    def boom(self, url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.Session.get", boom)
    assert tw_chip.get_monthly_revenue("6782") is None
