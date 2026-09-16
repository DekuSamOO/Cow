---
paths:
  - "core/**"
  - "BTC_WATCH.py"
  - "scripts/daily_line_notify.py"
  - "handler/tab_macro_compass.py"
  - "strategy/**"
  - "handler/components/backtest_walkforward.py"
  - "tests/core/**"
  - "tests/strategy/**"
  - "tests/test_alert_logic.py"
  - "tests/test_hedge_batch_alert.py"
---

# Cow：三大核心系統實作細節與指標／回測陷阱

> 自 `CLAUDE.md` 逐字搬出（2026-09-15，L1 瘦身）。編號沿用原編號，外部一律引標題；標題索引在 CLAUDE.md〈已知陷阱〉。

### 最低價綜合評估（四季論底部）

**單一真實來源 `core/bottom_floors.py::compute_all_bottom_estimates()`**，LINE 推播與
dashboard（tab D2.5）**共用同一函式**，杜絕兩邊算法漂移。
`final_low = max(四季論趨勢底, 礦工電費硬地板)`；ensemble = 強錨中位數。
組成與回測倍數 → FINDINGS No.1。

- **bitcoin-data.com 429 burst 限流**：連續 ~6 次即冷卻數分鐘。端點間隔 4s ＋ 遇 429 長退避
  ＋ **12h 持久化快取**。**勿密集探測。**
- on-chain 指標**僅約 4 年歷史**，只能驗證 2022 輪；2015/2018 靠礦工成本回測。
- `compute_all_bottom_estimates(now=...)` 須傳 **naive datetime**（tz-aware 會與 naive
  `HALVING_DATES` 比較拋錯）。

### 相對高/低點雷達（逃頂＋抄底）

**單一真實來源**：`core/relative_high.py`（逃頂五維）＋`relative_low.py`（抄底六維）＋
`trend_direction.py`（趨勢方向四維）。dashboard、`BTC_WATCH.py`、LINE 推播共用。

配重與 AUC → FINDINGS No.2~No.4；權重以 `WEIGHTS`／`WEIGHTS_LOW` 為單一真實來源，
**勿手動精算複寫**。

**`BTC_WATCH.py` 正本在本 repo 根目錄**（2026-06-10 起不再維護 Crypto repo 那份，該 repo
已無此檔）：OI 用 `openInterestHist`（5m×13 滾動清洗＋1d×30 分位）取代失效的相鄰 60s 差值；
防線用 `bottom_floors.final_low`（fallback 54000）；總經事件讀本地 `db/macro_events.json`，
**不打被擋的 FRED**。

- **⚠️ Farside ETF 佔位 0.0**：Farside 對「最新未定案日」回 `0.0`，當真實值存入會讓 streak
  邏輯把「連續 8 天機構流出」顯示成「🟢 淨流入」（完全反向、遮蔽逃頂訊號）。
  **交易日淨流量恰為 0.0 極罕見，一律視為當日無資料。**（`tests/test_etf_flow.py`）

### 改動守則（違反即 bug）

（「OI×Funding 假頂折減」「反指標整段移除」兩條留在 CLAUDE.md〈三大核心系統〉，此處只放改 WEIGHTS 的步驟）

- **改 WEIGHTS 必做三件事**：①重算 `BTC_WATCH.TOP_CAP`/`LOW_CAP`（**現為 99，勿用
  「100−7」捷徑**——clamp(100) 會蓋掉超編）；②**grep 所有 `_panel(...)` 呼叫端**確認
  `dims` tuple 與 `compute_relative_*` 回傳的 signals key **完全一致**（曾漏 `vol_price`/
  `structure`，分數算進總分卻不顯示，使用者無從判讀）；③三個消費端都要餵參數
  （`BTC_WATCH.py`、`tab_macro_compass::_gather_radar_externals`、
  `daily_line_notify::_compute_radars`）。

### 8. AHR999 冪律公式（舊版膨脹至 $177 萬）

```python
# ✅ Giovanni Santostasi 冪律
estimated_price = 10 ** (-17.01467 + 5.84 * np.log10(days_since_genesis))
# ❌ 舊線性指數模型（勿用）
estimated_price = 10 ** (2.68 + 0.00057 * days_since_genesis)
```

### 9. Walk-Forward 雙重移位

條件裡用了 `close_shifted` 再整體 `shift(1)` → 實際用到 2 天前資料。
**所有條件統一用當日值，最後一次性 `shift(1)`。**

### 10. Walk-Forward 進場乖離硬編碼 1.5% 上限

造成極少進場（ROI −22% vs swing +1654%）。已改可選參數 `entry_dist_max_pct`（預設 `None` 無上限）。

### 22. P4 重啟偵測曾掛在錯的觸發點上

`detect_mart_restart()` 原本只被 `notify_defense_line()` 呼叫，而後者只在價格跌破
`ALERT_PRICE_LOW` 才執行——**「要用防守階梯的那一刻，才發現階梯早就壞了」**。
2026-07-13 的對帳基線在 8/19–8/21 上漲後失效（兩台馬丁各重啟 6~9 輪），
價格從沒跌破警報價，偵測邏輯就從沒跑過，一個多月無人知曉，最後靠人工對帳發現。

2026-08-21 改由 `scripts/daily_line_notify.py::maybe_send_mart_restart_alert()` 每日驅動，
重啟後 24h 內告警（去重 key＝基線日＋已重啟名單，更新基線後可再告警）。
**通則：偵測器的觸發條件不可與「它要保護的那件事」同時成立**，否則等於沒有偵測。

> [!note] **2026-09-07：本偵測器已整組移除**（使用者拍板，見 README v3.51）。
> 條目與編號保留不重排（引用要引標題不引序號）。移除理由是它換一種方式重演了同一件事：
> 改成每日驅動之後**照樣沒出聲**——Actions 美國 IP 取不到行情，`detect_mart_restart`
> 恆回 `None`，呼叫端只 `print` 一行「略過」就結束，六場抽查全是這樣；
> 而兩台馬丁 08-24／08-25 就已重啟。
> **通則要再補一句：偵測器「取不到資料」時不可靜默**——
> 那和「偵測到沒事」是兩件事，處置也不同（同一系統的套保對拍守門就做對了：
> 分歧或對拍源不可得 → 不下單但**推警示**）。
>
> ✅ **2026-09-07 已落地為機制，不再靠自律**：
> `daily_line_notify._maybe_send_data_gap_alert` ＋ `_clear_data_gap_flag`，
> 套用在**會影響下單決策的四個哨兵**（升槓桿窗口／熊底 D3／D3 網格緩衝／套保建倉）。
> 推一次、恢復時清旗標（同 D3 死結告警的模式），一天三場不洗版。
> **逃頂雷達與合成行動刻意不納入**——SOP 附錄 F-4 已定為「不作賣出依據」，
> 替不會據以下單的分數推缺值警報只會淹掉真正該看的那幾則。
> 紅線測試在 `tests/test_alert_logic.py`（缺值必推／不洗版／恢復清旗標）
> 與 `tests/test_hedge_batch_alert.py`（舊斷言「缺值只能沉默」已被推翻並改寫）。
