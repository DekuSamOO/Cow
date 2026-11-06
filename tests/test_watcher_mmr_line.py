"""watcher._mmr_line：台股面板「大盤融資維持率」一列的顯示（純字串，零網路）。"""
import watcher


def test_full_line():
    s = watcher._mmr_line({"ratio": 164.66, "prev": 166.2, "prev_date": "2026-11-03",
                           "pctile": 72.4, "as_of": "2026-11-04"})
    assert "大盤融資維持率 164.7%" in s
    assert "前值 166.2%（11-03）" in s and "近一年百分位 72" in s
    assert "僅顯示不計分" in s and "含 ETF" in s


def test_missing_parts():
    s = watcher._mmr_line({"ratio": 150.0, "prev": None, "prev_date": None, "pctile": None,
                           "as_of": "2026-11-04"})
    assert "前值 —" in s and "近一年百分位 —（歷史不足）" in s


def test_none_shows_unavailable():
    assert "本輪抓不到官方檔" in watcher._mmr_line(None)
