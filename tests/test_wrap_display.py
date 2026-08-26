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


# ── 0x2600–0x27BF 區間的寬度判定（2026-08-26）──────────────────────────────────
# 教訓：這段「雜項符號與 Dingbats」原本被 _dw 一律當寬度 2，但**只有 EAW=W 的才真的寬**。
# 六個常用字元（⚠ ❄ ✕ ☀ ⚙ ✓）是 EAW=N、終端機只佔一欄 → 每出現一個就少補一格空白、
# 右框往左縮一欄。2026-08-26 使用者回報「LINE 哨兵」那行錯位，該行 4 個 ✕、右框少 4 欄。
# 之前只把 ⚠ 個案豁免（_NARROW_SYMBOLS={0x26A0}），治標不治本 —— 下一個符號照樣中招。
def test_dingbat_range_width_follows_east_asian_width():
    """0x2600–0x27BF 內：EAW=W/F → 2，其餘 → 1。**不可再改回整段一律 2。**"""
    import unicodedata
    from core.term_ui import _dw
    for ch in ("⚠", "❄", "✕", "☀", "⚙", "✓"):
        assert unicodedata.east_asian_width(ch) not in ("W", "F")
        assert _dw(ch) == 1, f"{ch} U+{ord(ch):04X} 應為窄符號，實得 {_dw(ch)}"
    for ch in ("⚪", "✅", "❌", "⛔", "❓", "⚡"):
        assert unicodedata.east_asian_width(ch) in ("W", "F")
        assert _dw(ch) == 2, f"{ch} U+{ord(ch):04X} 應為寬符號，實得 {_dw(ch)}"


def test_real_emoji_range_still_wide():
    """0x1F300–0x1FAFF 是真 emoji，維持一律 2（本次修正不得波及）。"""
    from core.term_ui import _dw
    for ch in ("🔴", "🟠", "🟡", "🟢", "📊", "📌"):
        assert _dw(ch) == 2, f"{ch} 應為寬度 2，實得 {_dw(ch)}"


def test_row_padding_closes_border_with_narrow_symbols():
    """含窄符號的整行經 _row 補白後，總顯示寬度必須 = W+2（左右框各 1）。

    這是使用者實際看到的症狀：右框沒對齊。直接斷言渲染後的寬度，
    比斷言 _dw 更貼近他看到的東西。
    """
    from core.term_ui import _dw, _row
    W = 100
    for content in (
        "  LINE 哨兵     逃頂 11/45✕  窗口 ✕  D3 ✕(+35%/57天)  套保 0/3(下批RSI<65)",
        "  哨兵狀態      行動 —｜週報 —｜馬丁 —",
        "  升槓桿哨兵    AHR999 0.529 [--]<0.40   ⚠ 已逾2h未更新",
    ):
        assert _dw(_row(content, W)) == W + 2, \
            f"補白後寬度 {_dw(_row(content, W))} != {W + 2}：{content!r}"
