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
from datetime import datetime, timedelta, timezone

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
    # 日期同樣相對今天算（與 `_data()` 同一個理由）：寫死的話對拍會被判成
    # 「落後 N 天」而走進分歧分支，這些測試就不是在測批次判定了。
    monkeypatch.setattr(notify, "crosscheck_daily_rsi",
                        lambda *a, **k: (0.0, 100.0, str(
                            datetime.now(timezone.utc).date() - timedelta(days=1))))
    return box


def _data(rsi_closed, peak=86.0, price=76992.0, closed_date=None):
    """哨兵吃的是收盤口徑的鍵；rsi14 是盤中值，故意給一個會誤觸的數字當陷阱。

    `rsi_closed_date` **必須相對今天算**：2026-09-11 起哨兵會擋下落後超過
    `HEDGE_MAX_CLOSED_BAR_LAG_DAYS` 天的收盤（見 tests/test_incomplete_last_day.py）。
    舊版這裡寫死 "2026-09-02"，守門一上線整個檔就會全紅。
    預設給「昨天」＝ `closed_daily_rsi()` 能拿到的最新值。
    """
    return {"rsi14": 30.0, "rsi14_closed": rsi_closed, "rsi_peak": peak,
            "rsi_closed_date": closed_date or str(
                datetime.now(timezone.utc).date() - timedelta(days=1)),
            "current_price": price}


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


def test_missing_rsi_alerts_instead_of_going_silent(notify, sent):
    """2026-09-07 推翻舊斷言「資料缺值時只能沉默略過」。

    舊行為與 P4 馬丁重啟哨兵的死法同型：取不到資料就靜默，於是「該響卻響不了」
    與「偵測到沒事」長得一樣。現在缺值改推一則「哨兵失明」警示（每次故障一次），
    但**不得推成建倉指示**——那才是「亂推」。
    """
    d = _data(63.9)
    d["rsi_peak"] = None
    notify.maybe_send_hedge_batch_alert(d)
    assert len(sent) == 1, "資料缺值不得靜默"
    text = sent[0]["text"]
    assert "哨兵失明" in text
    # 不可誤推成建倉指示——用正式建倉訊息的專屬字串判別，不用「建倉」二字
    # （告警標題本來就叫「套保建倉哨兵」，拿那兩個字判會自己咬自己）。
    assert "[套保建倉]" not in text and "全倉套保" not in text and "批觸發" not in text


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


# ── C. 狀態鏈斷裂守門（2026-09-11 立）──────────────────────────────────────────
# 立規原因：workflow 還原狀態時拿到 08-31 的舊 artifact（`gh run list --status success
# --limit 1` 回傳過期結果），`hedge_batch_1` 旗標消失 → 套保第 1 批在 09-09、09-10
# 兩個晚場**重複推了兩次建倉指示**。workflow 已改走 artifacts API，這裡守的是第二道。
#
# 三條紅線：舊狀態不可推建倉／舊狀態不可靜默／新鮮狀態不可被誤擋。
def _seed_state(notify, **kv):
    """寫一份「有內容但缺 hedge_batch_1」的舊狀態，模擬旗標被抹掉的情境。"""
    st = {"last_action_key": "RIDE", "score_history": {"2026-08-31": {"escape": 3, "low": 29}}}
    st.update(kv)
    with open(notify._ESCAPE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def test_stale_state_artifact_blocks_batch_alert(notify, sent, monkeypatch):
    """還原到十天前的 state → 已建過的批次會被當成沒建過，絕不可再推一次建倉。"""
    _seed_state(notify)
    monkeypatch.setenv("STATE_ARTIFACT_CREATED_AT", "2026-08-31T05:50:00Z")
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    texts = [p["text"] for p in sent]
    assert not any("[套保建倉]" in t for t in texts), \
        "還原到舊狀態時推建倉＝2026-09-09／09-10 重複推第 1 批的重演"


def test_stale_state_artifact_is_not_silent(notify, sent, monkeypatch):
    """不可靜默：擋掉建倉的同時必須說「狀態鏈斷了」，否則和『今天沒訊號』長得一樣。"""
    _seed_state(notify)
    monkeypatch.setenv("STATE_ARTIFACT_CREATED_AT", "2026-08-31T05:50:00Z")
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    assert len(sent) == 1 and "[狀態鏈斷裂]" in sent[0]["text"], \
        "狀態鏈斷裂必須推一則告警，不可默默跳過"


def test_fresh_state_artifact_does_not_block(notify, sent, monkeypatch):
    """新鮮的 artifact 不可被守門誤擋——誤擋等於把真正的建倉訊號吃掉。"""
    _seed_state(notify)
    fresh = (notify.datetime.now(notify.timezone.utc)
             - notify.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setenv("STATE_ARTIFACT_CREATED_AT", fresh)
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    assert len(sent) == 1 and "[套保建倉]" in sent[0]["text"]


def test_no_env_var_means_no_guard(notify, sent, monkeypatch):
    """本機／手動執行沒有這個環境變數，守門必須整段不生效（否則本機永遠推不出東西）。"""
    _seed_state(notify)
    monkeypatch.delenv("STATE_ARTIFACT_CREATED_AT", raising=False)
    notify.maybe_send_hedge_batch_alert(_data(63.9))
    assert len(sent) == 1 and "[套保建倉]" in sent[0]["text"]
