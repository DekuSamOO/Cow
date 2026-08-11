"""core/term_ui K 線疊圖均線測試（合成資料、零網路、確定性）。

疊圖的三個關鍵不變量：①右框線對齊（每列顯示寬度相同，色碼不計寬）②K 棒不被均線蓋掉
③框外均線不畫線但圖例照給數值——實測 MA200 有 61% 交易日落在近 30 日框外，硬 clamp
到邊緣會讓「年線遠在下方」看起來像「價格剛好站上年線」。
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from core.term_ui import (_kline_panel_lines, _dw_ansi, _ma_series, _pack_segs,
                          _print_with_kline, KLINE_MAS, KLINE_MAS_TW, kline_mas_for)

_N_DAYS = 30
_HEIGHT = 34


def _df(closes, spread=1.0):
    """close 序列 → OHLCV df（high/low 各外擴 spread，open=前一根 close）。"""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]), "high": c + spread,
        "low": c - spread, "close": c, "volume": [1_000_000] * len(closes),
    }, index=idx)


def _strip(s):
    """去 ANSI 色碼，讓字串索引＝畫面格位（逐格對拍用）。"""
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_all_lines_same_display_width():
    """右框線對齊：每列 _dw_ansi 必須相同。圖例列比格線寬時面板要跟著撐開，
    不能只撐圖例那一列（否則右框成鋸齒）。"""
    df = _df([100 + i * 0.5 for i in range(260)])
    lines = _kline_panel_lines(df, _N_DAYS, _HEIGHT)
    widths = {_dw_ansi(l) for l in lines}
    assert len(widths) == 1, f"寬度不一致：{sorted(widths)}"
    assert len(lines) == _HEIGHT      # 高度＝呼叫端指定值，供與左欄逐列並排


def test_in_frame_ma_is_drawn_and_labelled():
    """均線落在框內 → 格線畫得出該字元，且圖例無框外箭頭。"""
    df = _df([100 + i * 0.5 for i in range(260)])
    out = "\n".join(_kline_panel_lines(df, _N_DAYS, _HEIGHT))
    assert "·" in out and "MA5" in out          # 上升序列：MA5 貼著價格必在框內
    assert "↑" not in out.split("MA5")[1].split("\n")[0]


def test_off_frame_ma_not_drawn_but_legend_keeps_value():
    """MA200 遠在框下方（前 200 根低價墊底）→ 格線不出現 `=`，圖例仍給數值並標 ↓。
    這是刻意不 clamp 到框邊：貼邊會被讀成「價格剛好站上年線」。"""
    df = _df([50.0] * 200 + [100 + i * 0.2 for i in range(60)])
    lines = _kline_panel_lines(df, _N_DAYS, _HEIGHT)
    grid = [l for l in lines if "┤" in l or l[1:2] == " "]
    assert "=" not in "".join(grid)                       # 框外 → 不畫線
    legend = "\n".join(l for l in lines if "MA200" in l)
    assert "MA200" in legend and "↓" in legend            # 但圖例照給值＋方向
    assert "%" in legend                                   # 乖離%


def test_candle_wins_over_ma_in_same_cell():
    """同一格 K 棒優先：均線只能填空白格，絕不覆蓋價格字元（覆蓋就讀不出當天漲跌）。
    逐格對拍「疊圖版 vs 無均線版」——無均線版是 K 棒的格，疊圖後必須是同一個字元。
    用貼著均線的鋸齒序列（MA5/MA20 幾乎與 close 重合）確保大量撞格。"""
    df = _df([100.0 + (i % 2) * 0.5 + i * 0.01 for i in range(260)])
    lines = _kline_panel_lines(df, _N_DAYS, _HEIGHT)
    n_legend = sum(1 for l in lines if "MA" in l)
    assert n_legend >= 1
    # 無均線版高度扣掉圖例列 → 兩者 n_rows 相同，格點才對得起來
    bare = _kline_panel_lines(df, _N_DAYS, _HEIGHT - n_legend, mas=())
    n_rows = _HEIGHT - 3 - n_legend
    collisions = 0
    for a, b in zip(lines[1:1 + n_rows], bare[1:1 + n_rows]):
        a, b = _strip(a), _strip(b)
        for i in range(1, min(len(a), len(b)) - 1):   # 去頭尾框線
            if b[i] in "█│":
                assert a[i] == b[i], f"第 {i} 格 K 棒被均線蓋掉：{b[i]!r} → {a[i]!r}"
            elif a[i] in "·-=":
                collisions += 1
    assert collisions > 0        # 確實有畫到均線（否則這測試等於沒驗）


def test_ma_with_insufficient_history_is_skipped_entirely():
    """歷史不足以讓整段 n_days 都有值 → 該天期整條略過（不畫半截，也不進圖例）。
    半截均線會被讀成「均線在這天才開始」。"""
    df = _df([100 + i * 0.5 for i in range(120)])       # <200+30-1 → MA200 不可能整段有值
    assert [p for p, *_ in _ma_series(df, _N_DAYS, KLINE_MAS)] == [5, 20]
    out = "\n".join(_kline_panel_lines(df, _N_DAYS, _HEIGHT))
    assert "MA200" not in out
    assert "MA5" in out and "MA20" in out


def test_dw_ansi_ignores_colour_codes():
    """_dw 會把 ESC[92m 逐字元算進寬度；對齊與終端寬度判斷一律走 _dw_ansi。"""
    assert _dw_ansi("\x1b[92m█\x1b[0m") == 1
    assert _dw_ansi("abc") == 3


def test_pack_segs_wraps_by_display_width():
    segs = [("aaaa", "aaaa"), ("bbbb", "bbbb"), ("cccc", "cccc")]
    assert len(_pack_segs(segs, width=10)) == 2      # "aaaa  bbbb"=10 → 第三段換行
    assert len(_pack_segs(segs, width=100)) == 1


def test_print_with_kline_drops_chart_when_terminal_too_narrow(monkeypatch, capsys):
    """終端寬度以**側欄實寬**判斷（疊均線後圖例可能比格線寬）：放不下就整個不畫，
    退回單欄逐行印，絕不讓側欄溢出換行把整頁對齊毀掉。"""
    df = _df([100 + i * 0.5 for i in range(260)])
    left = [f"│{'x' * 40}│"] * _HEIGHT
    W = 40

    monkeypatch.setattr(os, "get_terminal_size", lambda *a: os.terminal_size((500, 50)))
    _print_with_kline(left, W, df, _N_DAYS)
    assert "MA5" in capsys.readouterr().out              # 夠寬 → 有側欄

    monkeypatch.setattr(os, "get_terminal_size", lambda *a: os.terminal_size((60, 50)))
    _print_with_kline(left, W, df, _N_DAYS)
    out = capsys.readouterr().out
    assert "MA5" not in out and out.count("\n") == _HEIGHT   # 太窄 → 原樣逐行印


def test_ma_set_differs_by_market():
    """分市場均線組：台股 5/20/60/240（週/月/季/年線），美股與幣沿用 5/20/200。
    根因是「根/年」不同（實測 台股 243、美股 251、幣 365）——同一個 MA200 在股市涵蓋
    9.6~9.9 個月、在幣市只有 6.5 個月；台股 243 根/年 → 年線是 240 不是 200。"""
    assert [p for p, *_ in kline_mas_for("tw_stock")] == [5, 20, 60, 240]
    assert [p for p, *_ in kline_mas_for("us_stock")] == [5, 20, 200]
    assert [p for p, *_ in kline_mas_for("crypto")] == [5, 20, 200]
    assert kline_mas_for("未知市場") is KLINE_MAS       # 未知類別不拋錯，回國際慣例組


def test_ma_chars_unique_within_each_set():
    """字元必須互異：`_enable_windows_ansi` 對舊主控台是靜默降級，顏色全失效時
    只剩字元能分辨哪條是哪條。"""
    for kind in ("tw_stock", "us_stock", "crypto"):
        chars = [c for _, c, _ in kline_mas_for(kind)]
        assert len(set(chars)) == len(chars), f"{kind} 均線字元重複：{chars}"


def test_tw_four_ma_panel_still_aligned():
    """台股是 4 條線 → 圖例多一列，面板右框線仍須對齊、高度仍等於指定值。"""
    df = _df([100 + i * 0.3 for i in range(320)])
    lines = _kline_panel_lines(df, _N_DAYS, _HEIGHT, mas=KLINE_MAS_TW)
    assert len({_dw_ansi(l) for l in lines}) == 1
    assert len(lines) == _HEIGHT
    out = "\n".join(lines)
    for tag in ("MA5", "MA20", "MA60", "MA240"):
        assert tag in out
