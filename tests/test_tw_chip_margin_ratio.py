"""service.tw_chip 大盤融資維持率（get_market_margin_ratio）單元測試；TWSE 全部 mock，零網路。"""
import datetime

import service.tw_chip as tw_chip


_FIELDS = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
           "買進", "賣出", "現券償還", "前日餘額", "今日餘額"]          # 融券欄名與融資重複（同官方）


def _margin_row(sym, today_lots):
    return [sym, "x", "0", "0", "0", "0", f"{today_lots:,}", "0", "0", "0", "0", "0", "999"]


def _install(monkeypatch, margin_rows, closes, loan_k, pad=101):
    """MI_MARGN ALL：彙總表（3 列，含融資金額）＋逐檔表（>100 列才會被當逐檔表，用 0 張補列）。"""
    tw_chip._cache.clear()
    tw_chip._mmr_cache.clear()
    rows = list(margin_rows) + [_margin_row(f"9{i:03d}", 0) for i in range(pad)]
    calls = []

    def fake_tables(endpoint, params):
        calls.append(endpoint)
        if endpoint == "afterTrading/MI_INDEX":
            return [{"fields": ["指數", "收盤指數"], "data": [["發行量加權股價指數", "1"]]},
                    {"fields": ["證券代號", "證券名稱", "收盤價"],
                     "data": [[k, "x", v] for k, v in closes.items()]}]
        if endpoint == "marginTrading/MI_MARGN":
            assert params["selectType"] == "ALL"
            summary = {"fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
                       "data": [["融資(交易單位)", "1", "1", "1", "1", "1"],
                                ["融券(交易單位)", "1", "1", "1", "1", "1"]]}
            if loan_k is not None:
                summary["data"].append(["融資金額(仟元)", "1", "1", "1", "1", f"{loan_k:,}"])
            return [summary, {"fields": _FIELDS, "data": rows}]
        raise AssertionError(endpoint)
    monkeypatch.setattr(tw_chip, "_fetch_twse_tables", fake_tables)
    return calls


def test_same_day_computed_once_and_failure_not_cached(monkeypatch, tmp_path):
    seed, live = str(tmp_path / "seed.csv"), str(tmp_path / "live.csv")
    calls = _install(monkeypatch, [_margin_row("2330", 10)], {"2330": "100"}, loan_k=None)
    assert tw_chip.get_market_margin_ratio("20261104", seed_path=seed, live_path=live) is None
    n_fail = len(calls)
    assert tw_chip.get_market_margin_ratio("20261104", seed_path=seed, live_path=live) is None
    assert len(calls) == 2 * n_fail                     # 失敗不快取：第二次照樣重抓
    calls = _install(monkeypatch, [_margin_row("2330", 10)], {"2330": "100"}, loan_k=500)
    first = tw_chip.get_market_margin_ratio("20261104", seed_path=seed, live_path=live)
    n_ok = len(calls)
    assert tw_chip.get_market_margin_ratio("20261104", seed_path=seed, live_path=live) is first
    assert len(calls) == n_ok                           # 同一交易日第二次不再抓官方檔


def test_ratio_includes_etf_same_scope_as_loan(monkeypatch):
    # 融資金額（分母）含 ETF 的融資 → 分子也要含 ETF，否則範圍不一致（2026-11-06 審查抓到的錯）。
    # 2330：10 張 × 1000 × 100 = 1,000,000；0050：5 張 × 1000 × 200 = 1,000,000；融資 1,000 仟元
    _install(monkeypatch, [_margin_row("2330", 10), _margin_row("0050", 5)],
             {"2330": "100.00", "0050": "200.00"}, loan_k=1000)
    ratio, n = tw_chip.compute_market_margin_ratio("20261104")
    assert n == 2
    assert abs(ratio - 200.0) < 1e-9                                  # 2,000,000 ÷ 1,000,000 × 100


def test_uses_margin_balance_not_short_balance(monkeypatch):
    # 逐檔表有兩個「今日餘額」欄（融資第 6 欄、融券第 12 欄），必須取第一個
    _install(monkeypatch, [_margin_row("2330", 10)], {"2330": "100"}, loan_k=500)
    ratio, _ = tw_chip.compute_market_margin_ratio("20261104")
    assert abs(ratio - 200.0) < 1e-9


def test_missing_source_returns_none(monkeypatch):
    _install(monkeypatch, [_margin_row("2330", 10)], {"2330": "100"}, loan_k=None)
    assert tw_chip.compute_market_margin_ratio("20261104") is None


def test_prev_pctile_and_live_append(monkeypatch, tmp_path):
    today = datetime.date.today()
    days = [today - datetime.timedelta(days=i) for i in range(200, 0, -1)]   # 200 筆歷史，早於今天
    seed = tmp_path / "seed.csv"
    seed.write_text("date,ratio" + chr(10) + "".join(
        f"{d.isoformat()},{150 + i * 0.1:.2f}" + chr(10) for i, d in enumerate(days)), encoding="utf-8")
    live = tmp_path / "sub" / "live.csv"
    _install(monkeypatch, [_margin_row("2330", 10)], {"2330": "100"}, loan_k=600)   # 166.67%
    ymd = today.strftime("%Y%m%d")
    r = tw_chip.get_market_margin_ratio(ymd, seed_path=str(seed), live_path=str(live))
    assert abs(r["ratio"] - 1_000_000 / 600_000 * 100) < 1e-9
    assert r["prev_date"] == days[-1].isoformat()
    assert abs(r["prev"] - (150 + 199 * 0.1)) < 1e-6
    # 歷史 150.0~169.9；≤166.67 的有 167 筆，加上當日共 168／201
    assert r["n_hist"] == 201
    assert abs(r["pctile"] - 168 / 201 * 100) < 1e-6
    assert live.read_text(encoding="utf-8").splitlines() == ["date,ratio", f"{today.isoformat()},166.67"]
    # 同日再算一次不重複追加
    tw_chip.get_market_margin_ratio(ymd, seed_path=str(seed), live_path=str(live))
    assert len(live.read_text(encoding="utf-8").splitlines()) == 2


def test_short_history_no_pctile(monkeypatch, tmp_path):
    today = datetime.date.today()
    seed = tmp_path / "seed.csv"
    seed.write_text("date,ratio" + chr(10) + f"{(today - datetime.timedelta(days=1)).isoformat()},150" + chr(10),
                    encoding="utf-8")
    _install(monkeypatch, [_margin_row("2330", 10)], {"2330": "100"}, loan_k=600)
    r = tw_chip.get_market_margin_ratio(today.strftime("%Y%m%d"), seed_path=str(seed),
                                        live_path=str(tmp_path / "live.csv"))
    assert r["pctile"] is None and r["prev"] == 150.0


def test_seed_file_shape():
    rows = tw_chip._read_mmr_csv(tw_chip.MMR_SEED_PATH)
    assert len(rows) > 2500
    assert min(rows) == "2016-01-04"
    assert all(100 < v < 250 for v in rows.values())
