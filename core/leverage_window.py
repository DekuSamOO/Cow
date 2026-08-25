# -*- coding: utf-8 -*-
"""
core/leverage_window.py — 升槓桿窗口與熊底確認的單一真實來源（2026-08-25 立）。

三處共用：scripts/daily_line_notify.py（LINE 哨兵）、BTC_WATCH.py（終端機視覺化）、
未來的 dashboard。數字正本：vault
`Work\\BTC幣本位網格去留評估\\升槓桿窗口執行清單.md`（附錄 B／D-1）。

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
              rebound_req=0.50, days_req=90):
    """熊底確認（D3＝PREREG 預簽定義）：自最低點反彈 >= 50% 且距最低點 >= 90 天。

    回測（三次熊底、限「自 ATH 已跌逾 30%」後才評判）：中位延遲 +99 天、
    **提早喊底 0/2**；而「站回 200 週均線」「自低點彈 30%」等快訊號提早 51~304 天、
    觸發價比真底高 40~197%。故採用 D3、不自創更快的條件。
    """
    if not low_since or not current_price:
        return {"ok": None}
    cur = float(current_price)
    reb = cur / float(low_since) - 1.0
    c1 = reb >= rebound_req
    c2 = (days_since_low or 0) >= days_req
    return {
        "ok": bool(c1 and c2), "c1": bool(c1), "c2": bool(c2),
        "rebound": reb, "rebound_req": rebound_req,
        "days": int(days_since_low or 0), "days_req": days_req,
        "low": float(low_since), "low_date": low_date_iso,
        "price_req": float(low_since) * (1.0 + rebound_req),
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
