# -*- coding: utf-8 -*-
"""
套保建倉哨兵（G3）測試 —— 2026-09-02 立。

**為什麼要有這支**：這個哨兵 2026-08-25 建立後從未有能力觸發 ——
`summary["rsi_max_90d"] = float(btc["RSI_14"]...)` 用了未定義的變數 `btc`，
NameError 被外層 `except Exception` 吞掉 → rsi_max 恆為 None →
`maybe_send_hedge_batch_alert` 每天都在第一個 guard 就 return。
畫面上的哨兵總覽是 BTC_WATCH 自己算的，所以顯示一切正常，**8 天沒人發現**。

因此本檔測兩件事：
  A. 行為：收盤口徑的 RSI 進來時真的會推、會去重、盤中值不會誤觸。
  B. 靜態：`core/` 與 `scripts/` 底下**任何一支**都不得再出現「讀取一個哪裡都沒綁定的
     全域名稱」—— 這是上面那個 bug 的**類別**，不是只有 `btc` 這一個實例。
"""
import ast
import dis
import glob
import importlib.util
import json
import os
import sys
import types

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.sentinel_board import HEDGE_BATCHES, HEDGE_G3_PEAK, HEDGE_G3_WINDOW

_NOTIFY_PATH = os.path.join(_REPO, "scripts", "daily_line_notify.py")


@pytest.fixture(scope="module")
def notify():
    spec = importlib.util.spec_from_file_location("daily_line_notify", _NOTIFY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sent(notify, tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_ESCAPE_STATE_FILE", str(tmp_path / "state.json"))
    box = []
    monkeypatch.setattr(notify, "send_line_message", lambda payload: box.append(payload))

    # 2026-09-03：哨兵新增「兩源對拍」守門（見 maybe_send_hedge_batch_alert docstring）。
    # 本檔測的是**批次判定與去重行為**，不是對拍本身，所以把對拍源固定成「與主源同調」
    # ——否則這些測試會去讀真實的 15m DB，變成**非確定性、隨行情漂移**的測試
    # （實際踩過：2026-09-03 真實對拍源回 65.26，把 rsi=63.9 的案例擋掉，兩個測試無故變紅）。
    # 對拍守門本身的紅線在 tests/core/test_hedge_crosscheck.py。
    monkeypatch.setattr(notify, "crosscheck_daily_rsi",
                        lambda *a, **k: (0.0, 100.0, "2026-09-02"))
    return box


def _data(rsi_closed, peak=86.0, price=76992.0):
    """哨兵吃的是收盤口徑的鍵；rsi14 是盤中值，故意給一個會誤觸的數字當陷阱。"""
    return {"rsi14": 30.0, "rsi14_closed": rsi_closed, "rsi_peak": peak,
            "rsi_closed_date": "2026-09-02", "current_price": price}


# ── A. 行為 ────────────────────────────────────────────────────────────────────
def test_fires_first_batch_when_closed_rsi_below_65(notify, sent):
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    assert len(sent) == 1, "G3 前提成立且收盤 RSI < 65，第 1 批必須推播"
    text = sent[0]["text"]
    assert "第 1 批" in text and "0.0428" in text
    assert "全倉套保" in text, "產品別是決策的一部分，訊息必須寫明"


def test_dedupes_second_run_same_day(notify, sent):
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    assert len(sent) == 1, "每批只推一次"


def test_uses_closed_bar_not_intraday(notify, sent):
    """回測（U2_expectation.py）建在收完的日線收盤上，盤中破門檻不是被驗證過的情境。"""
    notify.maybe_send_hedge_batch_alert(_data(66.0))   # rsi14=30.0 是盤中陷阱值
    assert sent == [], "收盤 RSI 66 未破 65，不可因為盤中值就推播"


def test_threshold_is_strict_less_than(notify, sent):
    notify.maybe_send_hedge_batch_alert(_data(float(HEDGE_BATCHES[0][1])))
    assert sent == [], "剛好等於門檻不觸發（規則是嚴格小於）"


def test_skips_when_g3_precondition_not_met(notify, sent):
    notify.maybe_send_hedge_batch_alert(_data(40.0, peak=float(HEDGE_G3_PEAK)))
    assert sent == [], "近 %d 日峰值未 >%d，G3 前提不成立" % (HEDGE_G3_WINDOW, HEDGE_G3_PEAK)


def test_skips_when_rsi_missing(notify, sent):
    d = _data(63.9)
    d["rsi_peak"] = None
    notify.maybe_send_hedge_batch_alert(d)
    assert sent == [], "資料缺值時只能沉默略過，不可亂推"


def test_one_batch_per_run_even_if_multiple_due(notify, sent):
    """一次只推一批，避免同日連發三則。"""
    notify.maybe_send_hedge_batch_alert(_data(45.0))
    assert len(sent) == 1
    assert "第 1 批" in sent[0]["text"]


def test_state_records_batch_and_date(notify, sent, tmp_path):
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert st["hedge_batch_1"] is True
    assert "hedge_batch_1_date" in st


def test_batches_come_from_single_source(notify):
    """門檻只能有一份正本（原本 sentinel_board 與 notify 各寫一份 65/55/50）。"""
    assert notify.HEDGE_BATCHES is HEDGE_BATCHES
    assert [thr for _, thr, _ in HEDGE_BATCHES] == [65, 55, 50]


def test_g3_window_matches_backtest():
    """
    20 日＝回測 V1_bottom_and_hedge.py:16 `rolling(20).max()` 的定義。
    實作一度寫成 90 日（比回測寬鬆、無證據支持）。要改這個值，先回頭重跑 V1/E3。
    """
    assert HEDGE_G3_WINDOW == 20


# ── B. 靜態：不得再讀取「哪裡都沒綁定」的全域名稱 ─────────────────────────────
def _loaded_globals(src, path):
    """從 bytecode 收 LOAD_GLOBAL/LOAD_NAME（含巢狀函式），與 AST 是兩套獨立機制。"""
    code = compile(src, path, "exec")
    names, stack = set(), [code]
    while stack:
        c = stack.pop()
        for ins in dis.get_instructions(c):
            if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
                names.add(ins.argval)
        stack += [k for k in c.co_consts if isinstance(k, types.CodeType)]
    return names


def _bound_anywhere(src):
    """檔案裡任何地方綁定過的名稱（寬鬆估計，寧可漏報也不誤報）。"""
    tree = ast.parse(src)
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name.split(".")[0]) for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            args = getattr(n, "args", None)
            if args:
                for a in [*args.args, *args.posonlyargs, *args.kwonlyargs,
                          args.vararg, args.kwarg]:
                    if a:
                        out.add(a.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global):
            out.update(n.names)
    return out


# 模組執行時由直譯器注入的名稱，不是「沒綁定」
# （`__annotations__` 來自模組層級的變數註解，例如 `X: int = 1`）
_INJECTED = {"__file__", "__name__", "__doc__", "__spec__", "__loader__",
             "__package__", "__builtins__", "__debug__", "__path__",
             "__annotations__"}


def test_no_undefined_global_names():
    """
    守衛範圍是 `core/` 與 `scripts/` **整個目錄**（掃到就算，不必登記檔名）。

    只釘住 `daily_line_notify.py` 與 `sentinel_board.py` 兩個檔的話，擋住的是這次
    踩到的那個實例，不是 bug 的類別 —— 下一支新腳本照樣可以寫錯變數名、照樣被
    外層 `except Exception` 吞掉。
    """
    import builtins
    offenders = {}
    for path in sorted(glob.glob(os.path.join(_REPO, "core", "*.py"))
                       + glob.glob(os.path.join(_REPO, "scripts", "*.py"))):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        missing = sorted(_loaded_globals(src, path) - _bound_anywhere(src)
                         - set(dir(builtins)) - _INJECTED)
        if missing:
            offenders[os.path.relpath(path, _REPO)] = missing
    assert not offenders, (
        f"以下檔案讀取了哪裡都沒綁定的名稱：{offenders} —— 這正是 2026-08-25 `btc` "
        f"那個 bug 的類別：NameError 會被外層 except 吞掉，哨兵靜默死掉不報錯。")
