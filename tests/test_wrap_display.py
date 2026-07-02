"""BTC_WATCH._wrap_display 續行縮排自動偵測測試（讓長列換行後跟第一行內容對齊）。"""
from BTC_WATCH import _wrap_display, _SCORE_PREFIX_RE, _dw


def test_score_panel_row_continuation_aligns_with_label_start():
    """"  25/25  Mayer ...；200週 ...；冪律 ..." 這種 _panel() 產生的分數列換行後，
    續行要對齊到 "Mayer" 開始的欄位（分數前綴結束處），不是固定縮 5 格。"""
    row = "  25/25  Mayer 🟢 低於2年均線×0.8 (極度低估)；200週 🟢 跌破200週均 (歷史絕對底)；冪律 🟢 貼近冪律支撐"
    lines = _wrap_display(row, 46)
    assert len(lines) >= 2
    m = _SCORE_PREFIX_RE.match(row)
    expect_indent = " " * _dw(m.group(1))
    for cont in lines[1:]:
        assert cont.startswith(expect_indent), f"{cont!r} 沒對齊到 {len(expect_indent)} 格"


def test_single_digit_score_padding_still_aligns():
    """單位數分數（如 0/20）右對齊會多一格 pad，續行仍須對到同一欄（曾經因此對不齊的迴歸測試：
    舊版正規表達式要求剛好 2 個前導空白，單位數多一格 pad 就偵測不到，退回錯誤的固定 5 格）。"""
    row = "   0/20  資費 ⚪ 多方付費/中性；OI ⚪ 無顯著清洗，近期無爆量現象"
    lines = _wrap_display(row, 30)
    assert len(lines) >= 2
    m = _SCORE_PREFIX_RE.match(row)
    assert m is not None, "單位數分數前綴（多一格 pad）應該仍能被偵測到"
    expect_indent = " " * _dw(m.group(1))
    for cont in lines[1:]:
        assert cont.startswith(expect_indent)


def test_trend_panel_row_uses_its_own_wider_prefix():
    """_panel_trend() 的前綴格式（"  -40/±40 "）比 _panel() 寬一欄，續行要對到它自己的前綴，
    不是硬套 _panel() 的欄位寬度。"""
    row = "  -40/±40 🔴 完全空頭排列，價格跌破所有均線 (價<SMA50<SMA200，趨勢確立)"
    lines = _wrap_display(row, 30)
    assert len(lines) >= 2
    m = _SCORE_PREFIX_RE.match(row)
    expect_indent = " " * _dw(m.group(1))
    assert lines[1].startswith(expect_indent)


def test_non_score_row_falls_back_to_default_indent():
    """沒有分數前綴的列（如 "→ ..."）偵測不到 pattern，退回舊的 5 格縮排（width 需留得下
    5 格 pad 才驗得出來；width 太窄時硬切會擠掉 pad，屬既有行為，非本次改動範圍）。"""
    row = "  → 空頭但長週期已到底部區：等趨勢轉正再進，勿純憑估值接刀"
    assert _SCORE_PREFIX_RE.match(row) is None
    lines = _wrap_display(row, 30)
    assert len(lines) >= 2
    assert lines[1].startswith("     ")   # 退回預設 5 格


def test_short_row_not_wrapped():
    row = "  10/30  短列"
    assert _wrap_display(row, 46) == [row]
