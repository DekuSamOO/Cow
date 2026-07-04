"""core/watch_plan.py 測試（E1 交易計畫檔）。純函數、不打網路。"""
import datetime
import json
import sys

import pytest

sys.path.insert(0, ".")
from core import watch_plan as wp  # noqa: E402

_LONG = {"direction": "long", "entry": [950, 970], "stop": 920,
         "targets": [1050, 1120], "size_pct": 15,
         "valid_until": "2026-07-31", "note": "回踩月線佈局"}


def test_parse_valid_long():
    p = wp._parse_one("2330", _LONG)
    assert (p.entry_low, p.entry_high, p.stop) == (950.0, 970.0, 920.0)
    assert p.entry_mid == 960.0
    # R = (1050-960)/(960-920) = 2.25（手算對照）
    assert p.r_multiple() == pytest.approx(2.25)
    assert not p.expired(datetime.date(2026, 7, 31))
    assert p.expired(datetime.date(2026, 8, 1))


def test_parse_valid_short():
    p = wp._parse_one("QQQ", {"direction": "short", "entry": [740, 745], "stop": 760,
                              "targets": [700, 680]})
    # R = (742.5-700)/(760-742.5) ≈ 2.43
    assert p.r_multiple() == pytest.approx((742.5 - 700) / (760 - 742.5))


@pytest.mark.parametrize("bad", [
    {**_LONG, "stop": 955},                      # long stop 未低於進場下緣
    {**_LONG, "targets": [1120, 1050]},          # 目標未遞增
    {**_LONG, "targets": [960]},                 # 目標未高於進場上緣
    {**_LONG, "direction": "buy"},               # 方向非 long/short
    {**_LONG, "valid_until": "07/31"},           # 日期格式錯
    {"direction": "long", "entry": [950, 970]},  # 缺 stop
])
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        wp._parse_one("2330", bad)


def test_load_plans_missing_file_is_normal(tmp_path):
    plans, errors = wp.load_plans(str(tmp_path / "nope.json"))
    assert plans == {} and errors == []


def test_load_plans_broken_json_collects_error(tmp_path):
    f = tmp_path / "watch_plan.json"
    f.write_text("{oops", encoding="utf-8")
    plans, errors = wp.load_plans(str(f))
    assert plans == {} and len(errors) == 1 and "解析失敗" in errors[0]


def test_load_plans_bad_entry_skipped_good_kept(tmp_path):
    f = tmp_path / "watch_plan.json"
    f.write_text(json.dumps({"2330": _LONG, "9999": {**_LONG, "stop": 999}}), encoding="utf-8")
    plans, errors = wp.load_plans(str(f))
    assert list(plans) == ["2330"] and len(errors) == 1 and "9999" in errors[0]


def test_cached_reload_on_mtime_change(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "_cache", {"path": None, "mtime": None, "plans": {}, "errors": []})
    f = tmp_path / "watch_plan.json"
    f.write_text(json.dumps({"2330": _LONG}), encoding="utf-8")
    plans, _ = wp.load_plans_cached(str(f))
    assert "2330" in plans
    import os
    new = json.dumps({"QQQ": {**_LONG, "entry": [700, 710], "stop": 680,
                              "targets": [780], "valid_until": None}})
    f.write_text(new, encoding="utf-8")
    os.utime(f, (0, 9_999_999_999))          # 強制 mtime 變化（同秒內改檔也要能偵測）
    plans, _ = wp.load_plans_cached(str(f))
    assert list(plans) == ["QQQ"]


def test_panel_rows_content_and_expiry_warning():
    p = wp._parse_one("2330", _LONG)
    rows = wp.plan_panel_rows(p, price=960.0, today=datetime.date(2026, 8, 2))
    assert "已過期" in rows[0]                          # 過期警示置頂
    joined = "\n".join(rows)
    assert "LONG" in joined and "950.00 ~ 970.00" in joined
    assert "R 2.2" in joined                            # 2.25 顯示一位小數
    assert "回踩月線佈局" in joined
    # 停損距現價 (920/960-1)= -4.2%
    assert "-4.2%" in joined
