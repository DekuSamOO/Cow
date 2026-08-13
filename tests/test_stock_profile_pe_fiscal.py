"""scripts/stock_profile._pe_fiscal_quarter 的跨源對帳（service.tw_chip 全部 mock，零網路）。

這支的價值全在「不一致時拒絕輸出」：PE 讀 climber DB，基期只有 TWSE/TPEx 有，兩者是不同
資料源。若它退化成無條件套用，報告會標上一個可能屬於別份 PE 的季別，而且**不會有任何錯誤
訊息**——靜默錯誤只能靠測試釘住，故四條分支各有一項。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from stock_profile import _pe_fiscal_quarter                                   # noqa: E402


def _valuation(**over):
    """TWSE 分支的典型回傳（6782，2026-08-12 實測值）。"""
    base = {"close": 207.5, "yield_pct": 4.05, "pe": 12.58, "pb": 2.94,
            "pe_fiscal_quarter": "2026Q2", "pe_fiscal_quarter_raw": "115/2"}
    base.update(over)
    return base


def test_matching_pe_applies_fiscal_quarter(monkeypatch):
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: _valuation())
    assert _pe_fiscal_quarter("6782", "2026-08-12", 12.58) == {
        "quarter": "2026Q2", "raw": "115/2", "note": None}


def test_cross_source_pe_mismatch_refuses_quarter(monkeypatch):
    """climber 12.58 vs TWSE 12.85 → 拒發基期。寧可「未知」，也不標可能屬於別份 PE 的季別。"""
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: _valuation(pe=12.85))
    r = _pe_fiscal_quarter("6782", "2026-08-12", 12.58)
    assert r["quarter"] is None
    assert r["raw"] == "115/2"                  # raw 仍保留，供人工判讀差在哪
    assert "跨源 PE 不一致" in r["note"]


def test_tolerance_boundary(monkeypatch):
    """門檻是差距 > 0.01：0.01 以內視為捨入雜訊放行，0.02 才判不一致。"""
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: _valuation(pe=12.59))
    assert _pe_fiscal_quarter("6782", "2026-08-12", 12.58)["quarter"] == "2026Q2"
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: _valuation(pe=12.60))
    assert _pe_fiscal_quarter("6782", "2026-08-12", 12.58)["quarter"] is None


def test_lookup_miss_marks_quarter_unknown(monkeypatch):
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: None)
    r = _pe_fiscal_quarter("9999", "2026-08-12", 12.58)
    assert r["quarter"] is None and r["raw"] is None
    assert "查無此檔" in r["note"]


def test_lookup_exception_does_not_break_whole_report(monkeypatch):
    """基期是加值欄位，抓不到不該讓整份報告掛掉——但必須留下 note 說明是查詢失敗。"""
    def boom(symbol, date):
        raise RuntimeError("network down")

    monkeypatch.setattr("service.tw_chip.get_valuation", boom)
    r = _pe_fiscal_quarter("6782", "2026-08-12", 12.58)
    assert r["quarter"] is None and r["raw"] is None
    assert "RuntimeError" in r["note"] and "基期未知" in r["note"]


def test_missing_pe_on_either_side_skips_reconciliation(monkeypatch):
    """任一邊 PE 缺值就無從比對 → 不因此丟掉基期（但也就沒有對帳保證）。"""
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: _valuation())
    assert _pe_fiscal_quarter("6782", "2026-08-12", None)["quarter"] == "2026Q2"
    monkeypatch.setattr("service.tw_chip.get_valuation", lambda s, d: _valuation(pe=None))
    assert _pe_fiscal_quarter("6782", "2026-08-12", 12.58)["quarter"] == "2026Q2"


def test_passes_compact_date_to_valuation(monkeypatch):
    """`chip.as_of` 是 `2026-08-12`，而 TWSE/TPEx 端點吃 `20260812`——中間要轉。"""
    seen = {}

    def spy(symbol, date):
        seen["symbol"], seen["date"] = symbol, date
        return _valuation()

    monkeypatch.setattr("service.tw_chip.get_valuation", spy)
    _pe_fiscal_quarter("6782", "2026-08-12", 12.58)
    assert seen == {"symbol": "6782", "date": "20260812"}
