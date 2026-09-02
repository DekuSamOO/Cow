"""
core/sentinel_board.py · 哨兵總覽（單一真實來源）

2026-08-25 建立。動機：LINE 哨兵目前有 7 個，但**它們的狀態只存在推播那一刻**——
在 watcher / BTC_WATCH 畫面上完全看不到「哪些已經響過、哪些還在待命、離觸發多遠」。
本模組把 `escape_alert_state.json` 的推播狀態與當下的live 條件併成一張表。

純顯示、零副作用：**只讀狀態檔，不寫、不推播**。推播邏輯仍只在
`scripts/daily_line_notify.py`（那支才有 send_line_message）。

7 個哨兵與其去重鍵：
  1 逃頂警報      escape_alert           分級門檻 config.ESCAPE_ALERT_TIERS
  2 行動翻轉      action_alert           last_action_key
  3 馬丁重啟      mart_restart_alert     last_mart_restart_key
  4 升槓桿窗口    leverage_window_alert  兩道閘門 + 批次計數
  5 熊底確認 D3   bear_bottom_confirm    d3_confirmed
  6 套保建倉      hedge_batch_alert      hedge_batch_1/2/3
  7 週報          weekly_summary         last_weekly_date
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

_COW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_COW, "escape_alert_state.json")

# ⚠️ 狀態檔**不在本機**：LINE 哨兵跑在 GitHub Actions，狀態靠 artifact 在 run 之間傳遞
# （workflow 從上一個成功 run `gh run download`，跑完再 upload，保留 2 天）。
# 2026-08-25 實測：本機 escape_alert_state.json 根本不存在 → 面板把「有紀錄」的哨兵
# 也印成「尚無紀錄」（實際 last_action_label=順勢持有、last_weekly_date=2026-08-23）。
# → 這裡用 gh CLI 把 artifact 抓成本機快取，並**區分「讀不到狀態」與「該鍵沒紀錄」**。
REMOTE_CACHE = os.path.join(_COW, "db", "cache", "escape_alert_state.json")
ARTIFACT_NAME = "escape-alert-state"
WORKFLOW = "daily_line_notify.yml"
REMOTE_TTL_SEC = 6 * 3600

HEDGE_BATCHES = ((1, 65, 0.0428), (2, 55, 0.0428), (3, 50, 0.0429))
HEDGE_G3_PEAK = 75          # G3 前提門檻值，完整定義見下方「RSI 判斷依據」
# 前提視窗天數。**20，對齊回測**：Work/BTC幣本位網格去留評估/scripts/V1_bottom_and_hedge.py:16
# 的 `hot = pd.Series(R).rolling(20).max()`，E3 掃描沿用同一個閘門。
# 2026-09-02 修正：原本實作寫 90 日，比回測寬鬆且無證據支持（E-1 表「G3 n=35」是
# 20 日窗算出來的）。改這個常數等於改策略觸發頻率，動它要先回頭重跑 V1/E3。
HEDGE_G3_WINDOW = 20

# RSI 判斷依據（三支呼叫端共用，勿各自實作 —— 實作就是下面的 closed_daily_rsi）：
#   指標 = 日線 RSI-14（Wilder，pandas_ta.rsi(close, 14)，core/indicators.py:40）
#   口徑 = **收完的日線收盤**，排除當日未收 K 棒（回測 U2_expectation.py 是 15m
#          重採樣成 1D、close 取當日最後一筆，從沒測過盤中進場）
#   前提 = 近 HEDGE_G3_WINDOW 日 RSI 曾 > HEDGE_G3_PEAK
#   觸發 = RSI 嚴格小於門檻（65/55/50），每批各推一次


def closed_daily_rsi(df, window: int = HEDGE_G3_WINDOW):
    """
    套保建倉哨兵吃的三個 RSI 值，回傳 `(rsi14_closed, rsi_peak, closed_date)`。

    口徑正本＝上方「RSI 判斷依據」，**呼叫端只傳日線 df，不得自行重算**。
    2026-09-02 抽出：`scripts/daily_line_notify.py` 與 `BTC_WATCH.py` 原本各手刻一份，
    而且兩份的長度守門已經不一致（前者沒有、後者 `len(df) >= window`）——
    「口徑只寫在註解裡、程式碼各自為政」正是這次 90→20 與 `btc`→`btc_df` 的同一種成因。

    資料不足（df 為 None／缺 RSI_14 欄／收完的日線不滿 window 根）一律回
    `(None, None, None)`：湊不滿整個視窗算出來的 peak 會低估，寧可讓哨兵報缺值略過，
    也不要拿一個偏低的峰值去判 G3 前提。
    """
    if df is None or "RSI_14" not in getattr(df, "columns", []):
        return None, None, None
    today = datetime.now(timezone.utc).date()
    closed = df[df.index.date < today]
    if len(closed) < window:
        return None, None, None
    return (float(closed["RSI_14"].iloc[-1]),
            float(closed["RSI_14"].tail(window).max()),
            str(closed.index[-1])[:10])


def _read_json(path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return None


def fetch_remote_state(timeout: int = 20) -> Optional[dict]:
    """
    用 gh CLI 從最近一個成功的 workflow run 下載狀態 artifact，落地成本機快取。
    需要 gh 已登入；任何失敗都回 None（呼叫端沿用舊快取），**不擋畫面**。
    """
    try:
        os.makedirs(os.path.dirname(REMOTE_CACHE), exist_ok=True)
        run_id = subprocess.run(
            ["gh", "run", "list", "--workflow", WORKFLOW, "--status", "success",
             "--limit", "1", "--json", "databaseId", "-q", ".[0].databaseId"],
            capture_output=True, text=True, timeout=timeout).stdout.strip()
        if not run_id:
            return None
        out_dir = os.path.dirname(REMOTE_CACHE)
        subprocess.run(["gh", "run", "download", run_id, "-n", ARTIFACT_NAME,
                        "-D", out_dir], capture_output=True, text=True, timeout=timeout)
        return _read_json(REMOTE_CACHE)
    except Exception:
        return None


def load_state(allow_remote: bool = False):
    """
    讀推播狀態（只讀不寫）。回傳 (state, source)：
      source="local"        本機有狀態檔（一般只在 CI 上才有）
      source="remote"       用的是 GH Actions artifact 快取（附抓取時間）
      source="unavailable"  兩者都沒有 → **不可把缺鍵解讀成「沒發生過」**
    allow_remote=True 時，快取超過 REMOTE_TTL_SEC 會嘗試用 gh 更新（失敗沿用舊的）。
    """
    local = _read_json(STATE_FILE)
    if local is not None:
        return local, "local"
    age = time.time() - os.path.getmtime(REMOTE_CACHE) if os.path.exists(REMOTE_CACHE) else None
    if allow_remote and (age is None or age > REMOTE_TTL_SEC):
        fetched = fetch_remote_state()
        if fetched is not None:
            return fetched, "remote"
    cached = _read_json(REMOTE_CACHE)
    if cached is not None:
        return cached, "remote"
    return {}, "unavailable"


def _mark(done: bool, unavailable: bool = False) -> str:
    if unavailable:
        return "❔ 狀態未知"
    return "✅ 已推" if done else "⏳ 待命"


def sentinel_rows(top_score: Optional[int] = None,
                  gate: Optional[dict] = None,
                  d3: Optional[dict] = None,
                  rsi14: Optional[float] = None,
                  rsi_peak: Optional[float] = None,
                  state: Optional[dict] = None,
                  allow_remote: bool = False) -> list:
    """
    回傳哨兵總覽的顯示行（list[str]）。所有參數皆選填 —— 取不到的項目照樣列出，
    標成「—」而不是整段消失（死項要看得見，這正是 2026-08-25 稽核的教訓）。
    """
    if state is None:
        st, source = load_state(allow_remote=allow_remote)
    else:
        st, source = state, "local"
    unavailable = (source == "unavailable")

    def _hist(key, label):
        """有狀態才敢說「尚無紀錄」；讀不到狀態時必須說讀不到，不可裝作沒發生過。"""
        if unavailable:
            return "狀態讀不到（在 GH Actions artifact）"
        v = st.get(key)
        return f"上次 {v}" if v else "尚無紀錄"

    rows = []

    # 1 逃頂警報
    try:
        from config import ESCAPE_ALERT_TIERS
        tiers = ESCAPE_ALERT_TIERS
    except Exception:
        tiers = ()
    if tiers and top_score is not None:
        hit = next((n for f, n in tiers if top_score >= f), None)
        floors = "/".join(str(f) for f, _ in tiers)
        rows.append(f"1 逃頂警報    分數 {top_score}／門檻 {floors}"
                    + (f"  🔴 {hit}" if hit else "  ⚪ 未達"))
    else:
        rows.append("1 逃頂警報    —")

    # 2 行動翻轉
    rows.append("2 行動翻轉    " + _hist("last_action_label", "行動翻轉"))

    # 3 馬丁重啟
    rows.append("3 馬丁重啟    " + _hist("last_mart_restart_key", "馬丁重啟"))

    # 4 升槓桿窗口
    if gate and gate.get("ok") is not None and gate.get("ahr") is not None:
        g1 = "✅" if gate.get("g1") else "✕"
        g2 = "✅" if gate.get("g2") else "✕"
        # ahr/dath 缺值時走下面的「—」分支：不可在 f-string 直接格式化 None（獨立檢核 🟡 No.10）
        rows.append(f"4 升槓桿窗口  AHR999 {float(gate['ahr']):.3f}{g1}  距ATH {gate.get('dath')}天{g2}"
                    + ("  🟢 開窗中" if gate.get("ok") else "  ⚪ 未開"))
    else:
        rows.append("4 升槓桿窗口  —")

    # 5 熊底確認 D3（含 2026-08-25 新增的 c3「仍在熊市」閘門）
    if d3 and d3.get("ok") is not None:
        c1 = "✅" if d3.get("c1") else "✕"
        c2 = "✅" if d3.get("c2") else "✕"
        c3 = "" if d3.get("c3", True) else "  ⚠c3未過(距ATH太近)"
        rows.append(f"5 熊底確認D3  反彈 {d3.get('rebound', 0) * 100:+.1f}%{c1}"
                    f"  距低 {d3.get('days', 0)}天{c2}{c3}  {_mark(st.get('d3_confirmed'), unavailable)}")
    else:
        rows.append("5 熊底確認D3  —  " + _mark(st.get("d3_confirmed"), unavailable))

    # 6 套保建倉（G3 前提 + 三批）
    if rsi14 is not None and rsi_peak is not None:
        armed = rsi_peak > HEDGE_G3_PEAK
        done = [n for n, _, _ in HEDGE_BATCHES if st.get(f"hedge_batch_{n}")]
        nxt = next((f"<{thr}" for n, thr, _ in HEDGE_BATCHES
                    if n not in done and rsi14 >= thr), None)
        pre = "G3✅" if armed else f"G3✕(近{HEDGE_G3_WINDOW}日峰 {rsi_peak:.0f}，需>{HEDGE_G3_PEAK})"
        rows.append(f"6 套保建倉    RSI {rsi14:.1f}  {pre}"
                    f"  已建 {'?' if unavailable else len(done)}/3" + (f"  下一批 RSI {nxt}" if nxt and armed else ""))
    else:
        done = [n for n, _, _ in HEDGE_BATCHES if st.get(f"hedge_batch_{n}")]
        rows.append(f"6 套保建倉    —  已建 {'?' if unavailable else len(done)}/3")

    # 7 週報
    rows.append("7 週報        " + _hist("last_weekly_date", "週報"))

    # 狀態來源與新鮮度：讀不到 vs 有紀錄，使用者必須分得出來
    if unavailable:
        rows.append("   ⚠ 推播狀態讀不到 → 上面的『已推/已建』一律不可信"
                    "（需 gh 已登入；watcher 進場畫面會自動抓，每 6 小時一次）")
    elif source == "remote":
        try:
            age_h = (time.time() - os.path.getmtime(REMOTE_CACHE)) / 3600
            rows.append(f"   （推播狀態來自 GH Actions artifact 快取，{age_h:.1f} 小時前抓取）")
        except Exception:
            rows.append("   （推播狀態來自 GH Actions artifact 快取）")
    return rows


def sentinel_compact(top_score=None, gate=None, d3=None,
                     rsi14=None, rsi_peak=None,
                     state: Optional[dict] = None, allow_remote: bool = False) -> list:
    """
    兩行橫向摘要版（給垂直空間吃緊的 BTC 儀表板用）。

    為什麼要有：完整版 7 列 + 標題 + 來源 = 10 列，而儀表板**改動前就已經 51 列**、
    本來就超過一般終端機高度；再加 10 列等於把表頭與即時行情推出畫面
    （2026-08-25 實測 51 → 61 列）。橫向沒撐開是因為兩欄區把 W 壓在 102 欄、
    哨兵最寬才 62 —— 那是運氣不是設計，所以這裡也一併把寬度壓在 102 以內。
    完整版留給 watcher 進場畫面（那頁沒有東西跟它搶垂直空間）。
    """
    if state is None:
        st, source = load_state(allow_remote=allow_remote)
    else:
        st, source = state, "local"
    unavailable = (source == "unavailable")

    def tick(ok):
        return "✅" if ok else "✕"

    # 第一行：四個「會觸發動作」的哨兵當下條件
    try:
        from config import ESCAPE_ALERT_TIERS
        floor = min(f for f, _ in ESCAPE_ALERT_TIERS)
    except Exception:
        floor = None
    esc = f"逃頂 {top_score}/{floor}{tick(top_score >= floor)}"         if (top_score is not None and floor is not None) else "逃頂 —"
    win = f"窗口 {tick(gate.get('ok'))}" if gate and gate.get("ok") is not None else "窗口 —"
    if d3 and d3.get("ok") is not None:
        bear = "" if d3.get("c3", True) else "⚠近ATH"
        d3s = f"D3 {tick(d3.get('ok'))}({d3.get('rebound', 0) * 100:+.0f}%/{d3.get('days', 0)}天){bear}"
    else:
        d3s = "D3 —"
    done = sum(1 for n, _, _ in HEDGE_BATCHES if st.get(f"hedge_batch_{n}"))
    if rsi14 is not None and rsi_peak is not None:
        nxt = next((thr for n, thr, _ in HEDGE_BATCHES
                    if not st.get(f"hedge_batch_{n}") and rsi14 >= thr), None)
        armed = "" if rsi_peak > HEDGE_G3_PEAK else "G3✕"
        hedge = f"套保 {'?' if unavailable else done}/3{armed}" + (f"(下批RSI<{nxt})" if nxt else "")
    else:
        hedge = f"套保 {'?' if unavailable else done}/3"

    # 第二行：純狀態類（不會有即時條件）+ 狀態來源
    if unavailable:
        line2 = "狀態讀不到（在 GH Actions artifact）→ 已推/已建不可信"
    else:
        act = st.get("last_action_label") or "—"
        # `[5:]` 是要把 "2026-08-26" 去掉年份成 "08-26"；**不可套在 fallback 上**
        # ——"—"[5:] 會切成空字串，畫面顯示「週報 ｜馬丁」中間憑空缺一塊（2026-08-26 修）。
        _wk = st.get("last_weekly_date")
        wk = _wk[5:] if _wk else "—"
        mart = "已推" if st.get("last_mart_restart_key") else "—"
        src = ""
        if source == "remote":
            try:
                src = f"｜狀態源 artifact {(time.time() - os.path.getmtime(REMOTE_CACHE)) / 3600:.1f}h 前"
            except Exception:
                src = "｜狀態源 artifact"
        line2 = f"行動 {act}｜週報 {wk}｜馬丁 {mart}{src}"

    # ⚠️ 標籤欄一律補到**顯示寬度 16**（含開頭兩個空白），與同區其他行對齊
    #    （現價／升槓桿哨兵／熊底確認 D3／哨兵狀態…全部是 16）。
    #    "LINE 哨兵" 含半形字母，字面看起來跟四個中文字一樣長、實際只有 15，
    #    原本補 6 格 → 17，整行右移一格。**改字串時用 core.term_ui._dw 量，不要目測。**
    return [f"  LINE 哨兵     {esc}  {win}  {d3s}  {hedge}",
            f"  哨兵狀態      {line2}"]
