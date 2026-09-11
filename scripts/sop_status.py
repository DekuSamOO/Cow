# -*- coding: utf-8 -*-
"""
scripts/sop_status.py · BTC 部位 SOP 現況一次查完（2026-09-11 立）

**為什麼要有這支**：每次問「套保第幾批可以發動了嗎」「窗口開了沒」，
都得重新翻 vault 規則、翻 sentinel_board、翻 GitHub Actions 狀態 artifact，
一輪下來十幾次工具呼叫。規則與口徑其實全都已經寫死在程式裡了，
這支把它們一次印出來，讓判讀只剩「看表」這一步。

口徑**全部沿用生產路徑**，本檔一行都不自己重算：
  · 主源 RSI / AHR999 / D3 / 開窗閘門 → `scripts.daily_line_notify.get_decision_data()`
  · 對拍源 RSI（15m 重採樣 1D，回測那一套）→ `core.sentinel_board.crosscheck_daily_rsi()`
  · 批次已推播狀態 → GitHub Actions 的 escape-alert-state artifact

規則正本 → vault `Literature Note/1a BTC部位SOP.md`。本檔只報數字，不下操作結論。

用法：python scripts/sop_status.py
"""
import json
import os
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from core.sentinel_board import (HEDGE_BATCHES, HEDGE_G3_PEAK, HEDGE_G3_WINDOW,
                                 closed_daily_rsi, crosscheck_daily_rsi)

ARTIFACT_NAME = "escape-alert-state"


def fetch_state_via_artifacts_api(timeout: int = 60):
    """
    直接查 artifacts API 取最新一份未過期的狀態，回 (state, 來源說明)。

    **不用 `gh run list --limit 1` 定位 run**：2026-09-09／09-10 兩晚實測那支 API
    會回傳過期結果（拿到 08-31 的 run），害 `hedge_batch_1` 旗標消失、
    套保第 1 批重複推播兩次。詳見 vault
    `Github/Cow/歷程/20260911fix_哨兵狀態鏈斷裂.md`。
    """
    try:
        jq = ('[.artifacts[] | select(.expired == false)] | sort_by(.created_at) '
              '| last | "\\(.id) \\(.created_at)"')
        meta = subprocess.run(
            ["gh", "api", "repos/:owner/:repo/actions/artifacts?name=%s&per_page=100" % ARTIFACT_NAME,
             "--jq", jq],
            cwd=_REPO, capture_output=True, text=True, timeout=timeout).stdout.strip()
        if not meta or meta == "null":
            return None, "查無未過期的 artifact"
        art_id, created_at = meta.split(" ", 1)
        zip_path = os.path.join(_REPO, "db", "cache", "_state_artifact.zip")
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
        with open(zip_path, "wb") as f:
            r = subprocess.run(
                ["gh", "api", "repos/:owner/:repo/actions/artifacts/%s/zip" % art_id],
                cwd=_REPO, capture_output=True, timeout=timeout)
            f.write(r.stdout)
        with zipfile.ZipFile(zip_path) as z:
            state = json.loads(z.read(z.namelist()[0]).decode("utf-8"))
        os.remove(zip_path)
        return state, "artifact %s（created_at %s）" % (art_id, created_at)
    except Exception as e:
        return None, "取得失敗：%s" % e


def g3_premise(df, window: int = HEDGE_G3_WINDOW):
    """回 (峰值, 最後一根 RSI>75 的日期, 前提最後有效的收盤日)；資料不足回三個 None。"""
    if df is None or "RSI_14" not in getattr(df, "columns", []):
        return None, None, None
    today = datetime.now(timezone.utc).date()
    closed = df[df.index.date < today]
    if len(closed) < window:
        return None, None, None
    win = closed["RSI_14"].tail(window)
    hot = closed["RSI_14"][closed["RSI_14"] > HEDGE_G3_PEAK]
    if hot.empty:
        return float(win.max()), None, None
    last_hot = hot.index[-1]
    # 前提＝「近 window 根收完日線內曾 >75」→ 最後一根 >75 之後第 window 根收盤仍算數
    return float(win.max()), str(last_hot)[:10], str(last_hot + timedelta(days=window - 1))[:10]


def main():
    from dotenv import load_dotenv
    load_dotenv()
    from core.indicators import calculate_ahr999, calculate_technical_indicators
    from scripts.daily_line_notify import (_migrate_hedge_batch_1_date,
                                           get_decision_data)
    from service.market_data import fetch_market_data

    data = get_decision_data()
    btc_df, _ = fetch_market_data()
    btc_df = calculate_ahr999(calculate_technical_indicators(btc_df))

    rsi, peak, cdate = closed_daily_rsi(btc_df)
    x_rsi, x_peak, x_date = crosscheck_daily_rsi()
    _, last_hot, premise_until = g3_premise(btc_df)
    state, state_src = fetch_state_via_artifacts_api()
    # 線上 artifact 要等下一輪排程跑完才會帶著校正值重新上傳，這裡先套一次，
    # 免得本檔在那之前還印著被重推洗掉的舊首推日。
    state = _migrate_hedge_batch_1_date(state or {})

    out = []
    out.append("=== BTC 部位 SOP 現況 ===")
    out.append("查詢時間（UTC）: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    out.append("狀態來源: %s" % state_src)
    out.append("")

    out.append("[行情]")
    out.append("  現價: %s" % data.get("current_price"))
    out.append("  收完日線: %s" % cdate)
    out.append("  AHR999: %s" % data.get("ahr999"))
    out.append("  cycle ATH: %s @ %s（距今 %s 天）"
               % (data.get("cycle_ath"), data.get("cycle_ath_date"),
                  data.get("days_since_ath")))
    out.append("")

    out.append("[套保 G3]")
    out.append("  主源 RSI14 : %s" % (round(rsi, 2) if rsi is not None else None))
    out.append("  對拍 RSI14 : %s（%s）" % (round(x_rsi, 2) if x_rsi is not None else None, x_date))
    out.append("  近 %d 日峰值: 主源 %s / 對拍 %s"
               % (HEDGE_G3_WINDOW,
                  round(peak, 2) if peak is not None else None,
                  round(x_peak, 2) if x_peak is not None else None))
    out.append("  前提: 最後一根 RSI>%d = %s，有效到收盤日 %s"
               % (HEDGE_G3_PEAK, last_hot, premise_until))
    for n, thr, qty in HEDGE_BATCHES:
        done = state.get("hedge_batch_%d" % n)
        both = (rsi is not None and x_rsi is not None and rsi < thr and x_rsi < thr)
        one = (rsi is not None and x_rsi is not None and (rsi < thr) != (x_rsi < thr))
        # 「已推播」不是「已建倉」：state 記的是哨兵喊的日子（推播成功後才寫），
        # 哨兵不知道你實際幾點下單。2026-09-11 由「已建（…）」改名——舊字面害人把
        # 推播日讀成建倉日，兩批實際都是使用者跑在哨兵前面幾小時建的。
        mark = "已推播（%s）" % state.get("hedge_batch_%d_date" % n) if done else (
            "兩源皆過" if both else ("兩源分歧→不可建" if one else "未達門檻"))
        out.append("  第 %d 批 <%d（%.4f BTC）: %s" % (n, thr, qty, mark))
    out.append("")

    out.append("[開窗／D3 出場閘門]")
    out.append("  AHR999 <0.40 ? %s" % _cmp_lt(data.get("ahr999"), 0.40))
    out.append("  距 ATH >=300 天 ? %s" % _cmp_ge(data.get("days_since_ath"), 300))
    out.append("  窗口狀態（state）: lev_window_open=%s, lev_closed_days=%s"
               % (state.get("lev_window_open"), state.get("lev_closed_days")))
    out.append("  D3 已確認 ? %s" % bool(state.get("d3_confirmed")))
    out.append("  本波低點: %s @ %s（距今 %s 天）"
               % (data.get("bear_low_since_ath"), data.get("bear_low_date"),
                  data.get("days_since_bear_low")))
    out.append("")

    out.append("[哨兵狀態檔其他鍵]")
    for k in sorted(state):
        if k == "score_history":
            hist = state[k] or {}
            # 印筆數與**日期跨度**，不只印最後一天：狀態鏈斷過的話筆數看起來還是滿的，
            # 洞卻完全看不出來（2026-09-11 實測 8 筆橫跨 17 天、中間缺 9 天）。
            days = sorted(hist)
            span = (_span_days(days[0], days[-1]) + 1) if days else 0
            gap = "  ⚠️ 有洞：缺 %d 天" % (span - len(days)) if span > len(days) else ""
            out.append("  score_history: %d 筆，%s ~ %s（跨 %d 天）%s"
                       % (len(days), days[0] if days else None,
                          days[-1] if days else None, span, gap))
            continue
        out.append("  %s = %s" % (k, state[k]))

    print("\n".join(out))


def _span_days(d1, d2) -> int:
    """兩個 YYYY-MM-DD 相距幾天；格式壞掉回 0（當成無法判斷跨度，不報洞）。"""
    try:
        return abs((date.fromisoformat(str(d2)[:10]) - date.fromisoformat(str(d1)[:10])).days)
    except Exception:
        return 0


def _cmp_lt(v, thr):
    return "資料缺" if v is None else ("YES (%.4f)" % v if v < thr else "NO (%.4f)" % v)


def _cmp_ge(v, thr):
    return "資料缺" if v is None else ("YES (%s)" % v if v >= thr else "NO (%s)" % v)


if __name__ == "__main__":
    main()
