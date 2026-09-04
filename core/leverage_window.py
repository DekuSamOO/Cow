# -*- coding: utf-8 -*-
"""
core/leverage_window.py — 升槓桿窗口與熊底確認的單一真實來源（2026-08-25 立）。

三處共用：scripts/daily_line_notify.py（LINE 哨兵）、BTC_WATCH.py（終端機視覺化）、
未來的 dashboard。數字正本：vault
`Literature Note\\1a BTC部位SOP.md`（2026-08-26 更名並移入 Literature Note，
原放 `Work\\BTC幣本位網格去留評估\\`、原名「升槓桿窗口執行清單」；附錄 B／D-1）。

本模組只做純計算，不做 IO、不推播，方便測試對拍。

── 為何要有 signal_days ─────────────────────────────────────────────────
分批回測（`D1_batching.py:24-31` 的 `i = k*gap`）的 gap 是**訊號日**索引，
不是日曆日：閘門暫時不成立的日子只是不計數，不會重置建構。
2026-08-23 版哨兵用「日曆日 + 開窗即重置 lev_batches_sent=1」，兩者不一致——
歷史上 2018 那段窗口被 2~6 天的小反彈切成 7 截，實作會重置 6 次、六批永遠投不完。
本模組改為累計 signal_days，close 不清零，只有離開整個熊市階段才重置。
"""
from __future__ import annotations

WINDOW_RESET_DAYS = 90   # 連續關窗超過此天數，視為換一個熊市階段 → 批次計數歸零
BEAR_DRAWDOWN = 0.30     # 「自 ATH 已跌逾 30%」之後才開始評判熊底（與回測同定義）

# D3 觸發當下的 c3 門檻。**2026-09-04 自 BEAR_DRAWDOWN 拆出並由 30% 調為 20%。**
#
# 為什麼要拆：原本 c3 直接重用 BEAR_DRAWDOWN，理由是「不引入新參數」。但這兩者管的
# 是不同的事——BEAR_DRAWDOWN 是「何時開始找低點」的資料前處理，c3 是「觸發當下仍在
# 熊市」的閘門，合理值不同。共用的副作用是**死結**：c1 要求 price >= low x 1.5、
# c3 要求 price <= ath x (1-c3)，當本輪熊市夠淺時兩者可行區間是空集合，D3 永遠不可能
# 成立。逐日重放 2017-08~2026-09，c3=30% 下共 594 天處於死結（含 2025-11-20 起至今）。
#
# 為什麼是 20%：
#   * **期望值選不出門檻**——以 BTC 幣數計價、H=730 天重放，c3 >= 2.5% 之後 E[幣數]
#     完全平坦（1.832x，到小數第三位相同）；只有 < 2.5% 會崩到 1.582x（2021-10-18
#     誤報漏進來、該輪強平）。門檻只能用結構理由選，不能用 EV 選。
#   * **20% 是死結曲線的膝點**：死結天數 20%→141、25%→382、30%→594；往下走則平緩
#     （20%→5% 只再省 39 天）。
#   * **誤報安全邊際 8 倍**：擋掉 2021-10-18 只需 2.5%。
#   * 歷史觸發集合：20% 下為 2019-04-02／2020-04-02／2023-02-19／2024-02-20，
#     多出的 2024-02-20 是 2023-02-19 確認後 456 天的重複觸發，生產端 `d3_confirmed`
#     只推一次，擋不擋都無害。
# 完整證據 → vault `Literature Note\\1b D3 死結與 c3 門檻研究.md`。
D3_TRIGGER_DRAWDOWN = 0.20


def find_bear_low(closes, ath_value, start_pos=0, drawdown=BEAR_DRAWDOWN):
    """自 ATH 之後、且「已跌逾 drawdown」的區間裡找最低收盤。

    回傳 `(低點收盤, 低點在 closes 的位置)`；區間內沒有任何一天跌破門檻就回 `(None, None)`。
    不加這道跌幅門檻的話，熊市初期價格還在高檔就會被當成「低點」而誤判 D3。

    **只回位置不回天數**是刻意的：兩個呼叫端的「距低點天數」基準不同——
    BTC_WATCH 用 K 棒位移（手上就是完整日線快取），daily_line_notify 用日曆天
    （cron 執行時該日 K 棒可能還沒收）。日線無缺漏時兩者相等，但基準該由呼叫端
    自己決定，本函式只負責「30% 門檻 + 取最低」這段真正會分歧的邏輯。
    """
    if ath_value is None or closes is None or len(closes) == 0:
        return None, None
    thresh = float(ath_value) * (1.0 - drawdown)
    lo_val, lo_pos = None, None
    for i in range(max(0, int(start_pos)), len(closes)):
        c = closes[i]
        if c is None or c > thresh:
            continue
        c = float(c)
        if lo_val is None or c < lo_val:      # 嚴格小於 → 同值取最早，與 idxmin 一致
            lo_val, lo_pos = c, i
    if lo_pos is None:
        return None, None
    return lo_val, lo_pos


def gate_status(ahr999, days_since_ath, ahr_max, min_days):
    """兩道閘門的當下狀態。回傳 dict；缺值時 ok=None（不可判定）。"""
    if ahr999 is None or days_since_ath is None:
        return {"ok": None, "g1": None, "g2": None,
                "ahr": ahr999, "dath": days_since_ath}
    g1 = ahr999 < ahr_max
    g2 = days_since_ath >= min_days
    return {"ok": bool(g1 and g2), "g1": bool(g1), "g2": bool(g2),
            "ahr": float(ahr999), "dath": int(days_since_ath)}


def trigger_price(price, ahr999, ahr_max):
    """瞬間跌到位時、AHR999 觸及門檻所需的價位。

    AHR999 = (P/SMA200) x (P/PowerLaw) 對 P 是二次式，故在 SMA200 與冪律線
    視為不變的短期近似下：P* = P x sqrt(ahr_max / ahr)。
    ⚠️ 這是**下界**：真跌下去 SMA200 會跟著下移、使 AHR999 降得比二次式慢，
    實際觸發價會比本值高（跌得越慢差越多）。用於顯示「還差多少」的量級，不作為下單價。
    """
    if not price or not ahr999 or ahr999 <= 0 or ahr_max <= 0:
        return None
    return float(price) * (float(ahr_max) / float(ahr999)) ** 0.5


def advance_batches(state, is_open, today_iso, batch_days, batch_count):
    """依『訊號日』推進分批計數。回傳 (新 state, 該發第幾批 or None, 事件)。

    事件字串：'open'（關→開）／'batch'（窗口內到期）／'close'（開→關）／None。
    state 需含（皆可缺）：lev_window_open、lev_signal_days、lev_batches_sent、
    lev_window_start、lev_last_open_date、lev_closed_days。
    """
    s = dict(state or {})
    was_open = bool(s.get("lev_window_open"))
    sig = int(s.get("lev_signal_days") or 0)
    sent = int(s.get("lev_batches_sent") or 0)
    closed_days = int(s.get("lev_closed_days") or 0)

    if is_open:
        # 同一天重複跑不重複計數（去重靠 lev_last_open_date）
        if s.get("lev_last_open_date") != today_iso:
            sig += 1
            s["lev_last_open_date"] = today_iso
        s["lev_signal_days"] = sig
        s["lev_closed_days"] = 0
        s["lev_window_open"] = True
        if not was_open:
            s.setdefault("lev_window_start", today_iso)
            if sent == 0:
                s["lev_batches_sent"] = 1
                return s, 1, "open"
            # 窗口重開但批次已在進行 → 不重置，續接（本模組與舊版最大差異）
            return s, None, "reopen"
        due = sent * batch_days
        if sent < batch_count and sig >= due:
            s["lev_batches_sent"] = sent + 1
            return s, sent + 1, "batch"
        return s, None, None

    # 關窗
    s["lev_window_open"] = False
    if was_open:
        s["lev_closed_days"] = 1
        return s, None, "close"
    closed_days += 1
    s["lev_closed_days"] = closed_days
    if closed_days > WINDOW_RESET_DAYS and sig > 0:
        s["lev_signal_days"] = 0
        s["lev_batches_sent"] = 0
        s.pop("lev_window_start", None)
        return s, None, "reset"
    return s, None, None


def d3_status(current_price, low_since, low_date_iso, days_since_low,
              rebound_req=0.50, days_req=90, cycle_ath=None,
              trigger_drawdown=D3_TRIGGER_DRAWDOWN):
    """熊底確認（D3＝PREREG 預簽定義）：自最低點反彈 >= 50% 且距最低點 >= 90 天。

    回測（三次熊底、限「自 ATH 已跌逾 30%」後才評判）：中位延遲 +99 天、
    **提早喊底 0/2**；而「站回 200 週均線」「自低點彈 30%」等快訊號提早 51~304 天、
    觸發價比真底高 40~197%。故採用 D3、不自創更快的條件。

    ── c3：仍在熊市（2026-08-25 新增）────────────────────────────────────────
    原定義只要求「過去存在一個自 ATH 跌逾 30% 的低點」，**沒有要求觸發當下仍在熊市**。
    用生產的 season_forecast.cycle_ath 逐日重放，D3 歷史觸發 5 次，其中
    **2021-10-18 在 62,010 觸發、距 cycle ATH 僅 −2.5%**，一個月後見 69k 大頂，
    隨後跌至 15,781（−74.6%）並於 2022-05-11 跌破當時所用的低點 ——
    照 SOP 那天要把全部 USDT 換成現貨，是最糟的時點。

    分辨點（觸發當下距 cycle ATH）——⚠️ 數字依重放的去重規則而異，兩種都列，結論一致：
        以「同輪只記首次（365 日去重）」重放：−74.6% / −64.4% / −64.1% / −22.6%
        以「不去重、逐次記錄」重放：        −74.6% / −70.8% / −64.1% / −64.3%
        誤報一律是 **−2.5%** —— **唯一在 ATH 附近觸發的那次**。
    ⚠️ 誠實標註：上面 −22.6% 那次（2024-02-20）**會被本閘門一併擋掉**，
       它是 2023-02-19 已確認底部之後 456 天的重複觸發；生產端哨兵只推一次
       （狀態 d3_confirmed），擋掉重複觸發無害。所以不是「完全分離」，
       而是「誤報一定被擋、被擋的另一次是無害的重複觸發」。
    → 加一道「觸發當下仍需距 cycle ATH >= trigger_drawdown」的閘門。

    ⚠️ **2026-09-04 更正：原本這裡重用 BEAR_DRAWDOWN(30%)「不引入新參數」，該決定已被推翻。**
    重用造成 c1 與 c3 互斥的**死結**（見下方死結偵測段），且 BEAR_DRAWDOWN 管的是
    「何時開始找低點」、c3 管的是「觸發當下仍在熊市」，本來就是兩件事。
    現已拆為獨立的 `D3_TRIGGER_DRAWDOWN = 0.20`（模組頂端有完整選值理由）。
    `find_bear_low` 仍用 BEAR_DRAWDOWN(30%)，**不受本次調整影響**。

    ── c3 門檻怎麼選（2026-09-04）──────────────────────────────────────────
    **不能用期望值選**：以 BTC 幣數計價、H=730 天重放四個週期，c3 >= 2.5% 之後
    E[幣數] 完全平坦（1.832x，到小數第三位相同），只有 < 2.5% 崩到 1.582x。
    平坦的原因是門檻唯一改變的 2024-02-20 排在同週期 2023-02-19 之後，
    而生產端只推一次 —— **門檻改了，實際動作的那一次沒變**。
    且整條曲線的台階由**單一事件**（2021-10-18 誤報）決定、n=4 週期，
    這個樣本數不足以分辨 20%/25%/30%。故改用結構理由（死結頻率 vs 誤報邊際）選 20%。
    完整掃描 → vault `Literature Note\\1b D3 死結與 c3 門檻研究.md`。

    cycle_ath=None → 不套用 c3（向後相容；但生產端一律要傳，否則等於沒有這道閘門）。
    """
    if not low_since or not current_price:
        return {"ok": None}
    cur = float(current_price)
    reb = cur / float(low_since) - 1.0
    c1 = reb >= rebound_req
    c2 = (days_since_low or 0) >= days_req
    dd = None
    c3 = True
    if cycle_ath:
        dd = cur / float(cycle_ath) - 1.0
        c3 = dd <= -float(trigger_drawdown)

    # ── 死結偵測（2026-09-04 新增）────────────────────────────────────────
    # c1 要求 price >= low x (1+rebound_req)；c3 要求 price <= ath x (1-trigger_drawdown)。
    # 這兩條的可行區間會是**空集合**——當本輪熊市夠淺（低點離 ATH 不夠遠）時，
    # 「從低點彈 50%」的價位已經漲出「距 ATH c3%」的熊市定義，D3 永遠不可能成立。
    # 逐日重放 2017-08~2026-09：c3=30%（舊值）下共 594 天處於此狀態，c3=20%（現值）降到 141 天
    # （2021-05~10、2021-12~2022-05、2025-11-20 起至今），**而哨兵從未說過**
    # ——它只報「反彈不足／天數不足」，看起來像是還在等，其實是等不到。
    # 這裡只**偵測與揭露**，不改變任何判定：ok 的計算完全不受影響。
    #   deadlock          : 目前 c1 與 c3 是否互斥
    #   deadlock_max_c3   : 要讓兩者有解，c3 門檻最大只能設到多少（1 - (1+reb_req)*low/ath）
    deadlock, max_c3 = None, None
    if cycle_ath:
        ath_f = float(cycle_ath)
        c1_floor = float(low_since) * (1.0 + rebound_req)   # c1 的最低可行價
        c3_ceil = ath_f * (1.0 - float(trigger_drawdown))      # c3 的最高可行價
        deadlock = bool(c1_floor > c3_ceil)
        max_c3 = 1.0 - (1.0 + rebound_req) * float(low_since) / ath_f

    return {
        "ok": bool(c1 and c2 and c3), "c1": bool(c1), "c2": bool(c2), "c3": bool(c3),
        "rebound": reb, "rebound_req": rebound_req,
        "days": int(days_since_low or 0), "days_req": days_req,
        "low": float(low_since), "low_date": low_date_iso,
        "price_req": float(low_since) * (1.0 + rebound_req),
        "drawdown_from_ath": dd, "drawdown_req": -float(trigger_drawdown),
        "cycle_ath": (None if cycle_ath is None else float(cycle_ath)),
        "deadlock": deadlock, "deadlock_max_c3": max_c3,
    }


def compact_rows(gate, d3, price, ahr_max, min_days, batch_days=None,
                 sent=None, batch_count=None, signal_days=None):
    """兩行橫向摘要（給 BTC_WATCH 的即時行情區用，不另開面板佔垂直高度）。"""
    def ok(b):
        return "OK" if b else "--"

    rows = []
    if gate.get("ok") is None:
        rows.append("  升槓桿哨兵    AHR999/距ATH 缺值，無法判定")
    else:
        tp = trigger_price(price, gate["ahr"], ahr_max)
        if gate["ok"]:
            # 開窗態的尾段必須夠短：兩欄面板把版面下限壓在 2*_MIN_COL_W+2，
            # 這兩行是塞在「即時行情」全寬區的閒置橫向空間裡，超寬就會撐開整個畫面。
            tail = "窗口開啟"
            if sent is not None and batch_count:
                tail += " {}/{}批".format(sent, batch_count)
            if signal_days is not None and batch_days:
                tail += "  訊號日 {}/{}".format(
                    signal_days, (sent or 0) * batch_days)
        elif tp and price:
            tail = "窗口關閉  需 {:+.1f}% -> ${:,.0f}".format(
                (tp / price - 1.0) * 100.0, tp)
        else:
            tail = "窗口關閉"
        rows.append(
            "  升槓桿哨兵    AHR999 {:.3f} [{}]<{:.2f}   距ATH {}天 [{}]>={}   {}".format(
                gate["ahr"], ok(gate["g1"]), ahr_max,
                gate["dath"], ok(gate["g2"]), min_days, tail))
    if d3.get("ok") is None:
        rows.append("  熊底確認 D3   低點資料不足")
    else:
        # 一定要把「低點的價與日期」印出來：反彈幅度與 price_req 都是相對它算的，
        # 少了這個錨點，讀者會把 price_req 誤讀成「熊底要在這個價位」（實際是
        # 「站上這個價才算確認那個低點是真底」）。2026-08-25 使用者實際踩過這個誤解。
        d = d3.get("low_date") or ""
        rows.append(
            "  熊底確認 D3   低點 ${:,.0f} ({})  自低點 {:+.1f}% [{}]>=+{:.0f}%"
            "（需 ${:,.0f}）  距今 {}天 [{}]>={}".format(
                d3["low"], d[5:] if len(d) >= 10 else d,
                d3["rebound"] * 100, ok(d3["c1"]), d3["rebound_req"] * 100,
                d3["price_req"], d3["days"], ok(d3["c2"]), d3["days_req"]))
    return rows


def d3_grid_plan(lower_bound, open_price, grid_step, grid_count, liq_mult):
    """D3 觸發後的 2X 網格參數（`1a BTC部位SOP.md` 情境二 F-2）。

    下限＝前波新低本身；上限＝下限 x grid_step^grid_count；強平價為**粗估**
    （開單價 x liq_mult）。⚠️ liq_mult 不可信到能省略 App 覆蓋這一步——
    No.6 名義 L 該是 0.667、App 實際強平價落在 0.554（見 config_private.py 附註），
    差距達 17%。本函式只給「先算個數量級」的估算，開單後仍必須讀 App 訂單詳情
    的實際強平價比對，差 > 1% 就停下（SOP 步驟 5，本函式不重複那段流程）。
    """
    if lower_bound is None or open_price is None:
        return None
    lower = float(lower_bound)
    upper = lower * (float(grid_step) ** int(grid_count))
    return {
        "lower": lower,
        "upper": upper,
        "grid_count": int(grid_count),
        "liq_estimate": float(open_price) * float(liq_mult),
    }
