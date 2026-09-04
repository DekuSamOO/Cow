# CLAUDE.md — Cow（BTC 投資戰情室）

**路徑：** `D:\Users\63191\Documents\GitHub\Cow`　**Live：** https://mfyyo9qf5mymsrouxkfdgj.streamlit.app

> **📌 否決史唯一正本**：vault `Literature Note\4b Cow 開發決策史.md` 第三節。
> 本檔與 AUDIT/STRESS 各檔的否決敘述僅為摘要；**復活禁令查核一律查該表，新否決先入該表再引用**。
> **架構決策** → `_governance\ADR\Cow\`（ADR-001~004）
> **子系統規格** → `_governance\SPEC\cow-radar-spec.md`（每季隨 PREREG §0 第 5 點 (d) 校準）
> **回測數據正本** → `_governance\FINDINGS-cow-radar-backtests.md`
> **台股/美股資料源細節** → `docs\tw-us-data-sources.md`
> 版本與部署狀態以 README 為準，本檔不複述。

---

## 執行指令

```bash
# 本地開發（必須用 Anaconda Python）
D:\Users\63191\AppData\Local\anaconda3\python.exe -m streamlit run app.py

# 增量更新 BTC K 線並推送 GitHub
D:\Users\63191\AppData\Local\anaconda3\python.exe collector/btc_price_collector.py --push
```

排程 `Cow OI Snapshot`（09:00，跑 `collector\run_oi_snapshot.bat` → `--year <今年> --push`）
判斷有沒有跑，看 `LastRunTime` **不要看 `LastTaskResult`**（詳 `_governance\OPS-notes.md`）。

> [!warning] 本 repo 有每日自動 push 通道——**留在本地的 commit 會被它一併送上遠端**
> `git_push()` 的 commit **已精準鎖在 `db/`**（`git add db/`；`db/` 無變更就直接 return，
> 不 commit 也不 push），這部分沒問題。問題在最後一步 **`git push` 推的是「整個分支」
> 不是「剛才那個 commit」**——這是 git 的本質，改不掉。
>
> 所以只要本地有任何未推的 commit 躺著，**隔天 09:00 排程就會替你決定把它推出去**。
> 2026-08-06 實際發生過：一筆文件 commit 被當日的 K 線更新順手帶上 `origin/main`。
>
> **紀律**：在 Cow **不要留「還沒想清楚、暫時不想推」的本地 commit**。
> 真要暫存未定案的工作，用 `git stash` 或另開分支，不要 commit 到 `main` 上放著。
> （2026-08-06 評估過「push 前檢查是否有非資料 commit，有就只警告不推」的方案，
> **否決**——會讓「價格資料每天上雲」這個核心功能變得不可靠，為罕見情況犧牲天天要用的東西。）

> [!danger] 本機跑推播腳本**曾經**會真的發到使用者手機——已加閘門，但要知道為什麼
> 2026-09-02：一個 subagent 在本機直接跑 `python scripts/daily_line_notify.py`，
> **真的把當日完整 Flex 卡片推到使用者手機**，它以為那只是演練。
> 成因：`__main__` 沒有 dry_run 參數，且憑證由 `.env` 的 `load_dotenv()` 自動載入
> ——「在本機試一下」在這支腳本裡等於真的送出去。
>
> ✅ **已修（2026-09-04）**：`service/notification/core.py::_outbound_allowed()` 是
> **所有**對外推播的單一閘門（日常 LINE／防守 LINE／Telegram 三條路都走它）：
>
> | 環境 | 行為 |
> |---|---|
> | 本機（無 `GITHUB_ACTIONS`、未設 `DRY_RUN`）| **擋下**，印出擋下原因與內容摘要 |
> | GitHub Actions | 照送（`GITHUB_ACTIONS` 由 runner 自動設）|
> | 明確 `DRY_RUN=0` | 允許真送——**這個顯式性本身就是「核准」** |
> | `DRY_RUN` 任何其他非空值 | 擋下（CI 上也能演練）|
>
> 測試 `tests/test_outbound_gate.py` 18 項，負向驗證：停用閘門後 8 項失敗。
> **不要為了方便把閘門拿掉**——全域規則 §0.4 是建議性的，這道閘門才是確定性的。

---

## service 層 fallback chain（讀 code 看不出順序，改動前必看）

```
歷史K線：本地DB → Yahoo → Binance → Kraken → CryptoCompare（五層）
即時價格：Binance 現貨 → Kraken Ticker → 本地 15m DB 最新一筆（三層）
宏觀：FRED CSV → Yahoo → FRED 備援 → 靜態 _FALLBACK（四層）
```
Kraken Ticker 端點 `api.kraken.com/0/public/Ticker?pair=XBTUSD`，取 `result['XXBTZUSD']['c'][0]`。
資金費率三層見〈資金費率即時備援鏈〉；台股籌碼走 `tw_chip.get_chip_bundle`。
`db/` 為年度分割 SQLite（`btcusdt_15m_YYYY.db`），**雲端直接讀 repo 內 db**。

**來源追蹤慣例**：`fetch_realtime_data()` 回傳 dict 含 `price_source`／`funding_rate_source`／
`tvl_source`；UI 層直接讀 `rt.get('price_source', '歷史收盤')`，**不在 UI 層做 `is not None`
判斷**（leaky abstraction）。`get_latest_local_price()` 不帶快取供即時備援；
`read_btc_15m()` 有 `ttl=86400`，**不可**用於即時價格。

---

## 三大核心系統

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

**三軸要合看**：可同時「強多頭＋逃頂高」或「空頭＋抄底高」——**勿純憑估值接刀**。
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

- **⚠️ OI×Funding 假頂折減仍 NOT VERIFIED**（樣本不足非網路問題）：只准折減不准灌分、
  OI 無資料不折減。**不得移除、也不得轉正**。
- **反指標整段移除、不留參考顯示**（Hash Ribbons、TDCC major_pct 前例）。勿加回。
- **改 WEIGHTS 必做三件事**：①重算 `BTC_WATCH.TOP_CAP`/`LOW_CAP`（**現為 99，勿用
  「100−7」捷徑**——clamp(100) 會蓋掉超編）；②**grep 所有 `_panel(...)` 呼叫端**確認
  `dims` tuple 與 `compute_relative_*` 回傳的 signals key **完全一致**（曾漏 `vol_price`/
  `structure`，分數算進總分卻不顯示，使用者無從判讀）；③三個消費端都要餵參數
  （`BTC_WATCH.py`、`tab_macro_compass::_gather_radar_externals`、
  `daily_line_notify::_compute_radars`）。

### 台股/美股版（watcher 股票分支）

加密雷達的 funding/OI/鏈上維度股票無對應 → 台股改用**籌碼/估值**，美股用**純 OHLCV 通用軸**。
端點、欄序、TPEx fallback、TDCC 爬法 → **`docs\tw-us-data-sources.md`**。

**只有下列會害人犯錯的行為守則留在這裡：**

1. **`.TW`→`.TWO` 上櫃備援：日線與即時報價要各自套用，勿漏放一邊。**
   `fetch_live_quote` 曾漏掉，導致所有上櫃股現價/成交量永遠 404。2026-07-03 已抽出共用
   `_tw_candidates()`；**日後新增台股 fetch 函式務必套用同一份候選清單，不要各自複製判斷**。
2. **TWSE 日檔是 EOD 公布 → 必須 walk-back**：呼叫端常傳「今日」，但今日檔尚未出、連假整週
   無檔 → 三日檔會整片 None。用**單一探針**（BWIBBU 非空）往前找最近已公布交易日
   （lookback≤7），三源對齊同一 `as_of`。**用單一探針而非多源×多日盲掃**，避免撞限流。
3. **台股盤中報價法定延遲 ~20 分鐘，勿用 timestamp 新舊判斷盤中/收盤**：改用是否為交易時段
   （`_is_tw_trading_hours()`）。用 age<15 分鐘猜對美股成立、對台股永遠誤判成「已收盤」。

---

## 已知陷阱（跨功能通用）

> **序號會隨增刪漂移——本檔外部一律引「標題」不引序號。** 2026-08-10 清過一輪：治理文件 8 處
> 序號引用已全改標題（歷史 plan／AUDIT 刻意留原樣）。刪條目時**留占位不重編**。

### 1. `@st.fragment` 靜默失效（現價停止自動更新）

`@st.fragment(run_every=60)` 傳入 DataFrame/Series 時序列化失敗，**fragment 停止重跑但不報錯**。
→ 只傳 float scalar：`render(prev_close=float(...), rsi14=float(...))`。

### 2. `@st.cache_data(ttl=60)` + `run_every=60` 衝突

fragment 60 秒重跑、TTL 也 60 秒 → 永遠命中快取 → 數據不刷新。
`fetch_realtime_data()` 不掛 `@st.cache_data`。

### 3. 分辨「fragment 沒跑」還是「連線問題」（公司網路是 SSL 攔截，非封鎖）

看 fragment 內「數據更新時間」有無每分鐘更新：沒更新＝fragment 停跑（見〈`@st.fragment` 靜默
失效〉／〈`@st.cache_data(ttl=60)` + `run_every=60` 衝突〉）；有更新但值不動才是連線問題。
SSL 攔截通則與 curl 解法見全域 `~\.claude\CLAUDE.md` §6，備援鏈見〈service 層 fallback chain〉。

### 4. service 層來源追蹤慣例

→ 已併入〈service 層 fallback chain〉。**編號保留占位，勿重編。**

### 5. `reindex(method='nearest')` 早於資料起點填充定值

`fund_hist` 從 2021 起、`chart_df` 從 2015 起 → 2021 年前會填成第一筆值（常數線）。
手動清除：`fund_sub.loc[fund_sub.index < fund_hist.index[0]] = np.nan`

### 6. 資金費率即時備援鏈

Binance `fapi/v1/premiumIndex`（`lastFundingRate`）→ Bybit `v5/market/tickers`
（`result.list[0].fundingRate`）→ OKX `api/v5/public/funding-rate`（`data[0].fundingRate`），皆 ×100。

### 7. 圖表欄位名與顯示標籤混淆

`EMA_20` 直接當圖例易被誤讀為 `SMA 20` → `_ma_label(col)` 轉 `EMA 20`。
`exit_ma_key == 'EMA_20'` 時進場線與防守線同一條，合併標籤 `"EMA 20 (進場 ＆ 防守線)"`。

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

### 11. 派網 Bot API 不支援幣本位網格與馬丁格爾

`buOrderTypes` 只有 `futures_grid`／`spot_grid`／`smart_copy`；App 手動建的機器人不會出現在
API 回傳，帳戶餘額 API 也不含機器人內資產。→ **派網 API Key 對本專案無用，勿再嘗試。**

### 12. 新聞來源限制

Reddit `hot.json` 對公司 IP 與雲端共享 IP 均回 403（IP 層級，換 UA 無效）→ 改用
CoinGecko `/search/trending`。X 免費 API 已關閉，不納入。
媒體源：CryptoCompare News ＋ Cointelegraph/CoinDesk/Decrypt RSS（`service/news.py`）。

### 13. 新聞中文化省 token 三層（`service/news_i18n.py`）

批次一次 prompt 處理最多 8 則回 JSON ＋ 持久化快取 `db/news_i18n.json`（翻過的**永不重翻**，
記憶體快取 cold start 會清空故需落地）＋ 總開關 `NEWS_I18N_ENABLED=false`。

### 14. Gemini 兩坑

- 2.5 系列是 reasoning 模型，**預設 thinking 會吃光 `maxOutputTokens`** → 症狀是耗時久且回傳空。
  翻譯/摘要用 `generationConfig.thinkingConfig.thinkingBudget = 0`（`core/gemini_client.py`）。
- `ListModels` 會列出已下架模型（`gemini-2.0-flash` 實打回 404）。**不要以列出就當可用。**

### 15. requirements 勿替 numpy/pandas 加上限

pandas-ta 只有 pre-release（0.4.x）且依賴 `pandas>=2.3.2`。曾誤鎖 `numpy<2`+`pandas<2.3`
→ 雲端 uv 報 `No solution`、pip fallback source-build pandas 卡死、**app 起不來**
（原本「全無 pin」反而正常）。
**正解**：`pandas-ta==0.4.71b0`（pin 確切 pre-release，uv 才願解析）、`pandas>=2.3.2`、
`numpy>=1.26`，**一律不加上限**。鎖版本前先看雲端 build log，不可憑記憶臆測。

### 16. 檔案頂層 import 會拖進重依賴

`service/macro_data.py` 頂層 `import streamlit`+`yfinance`，但 `get_next_macro_event()` 只讀本地
JSON。**只要 import 那個檔案就會連帶 import streamlit**，公司網路下這個 import 動作本身會卡住
逾時（實測 >10 秒，**try/except 攔不到「卡住」**）。已抽出零依賴的 `service/macro_events.py`。
**教訓**：新增純邏輯函式前，先看它要放的檔案頂層 import 了什麼。

### 17. `_df_from_sqlite` 曾強制欄名全轉小寫

只為相容 yfinance `'Date'`/`'date'`，卻把 `fundingRate` 這種**資料欄**也轉小寫 → 消費端讀不到、
增量 concat 產生大小寫分裂雙欄、`to_sql` 因 SQLite 欄名大小寫不敏感 `duplicate column name` 崩潰。
修法：**只在 `index_col` 找不到時**才嘗試不分大小寫改名。
詳 `_governance\AUDIT-data-internals-batch4.md` C-17~C-19。

### 18. 分位型維度的「母體長度」是口徑的一部分

`vol_pctile` 拿最新值對 `fetch_ohlc` 抓回的**整段**歷史排名 → **抓多長＝母體多大＝分位定義**。
校準腳本用 expanding、面板自 2016-01-01 起，live 若只抓 2 年就是拿短記憶母體套長記憶門檻。
6782 實例：同一筆近5日均量，2 年母體 93.5 分位、10 年 83.5 分位，量能維 12/18 vs 6/18。
→ **`fetch_ohlc` 預設 `rng="10y"`，改短即改維度定義。**
**且不可用 `rng="max"`**：Yahoo 靜默降頻成週/月線（2330 max 只回 320 根、間隔 31 天），
欄名不變、無錯誤，分位會變成拿週量比日量。

### 19. Yahoo 台股「有價無量」幽靈列

volume=0 但 OHLC 正常、當天實際有成交（近 10 年 6782 1／2454 4／1101 7／6509 9 筆，
美股與幣對 0 筆）。混進量能母體、也把含它的 N 日均量整段拉低。
`fetch_ohlc` 已轉 NaN（**不刪列**——價格那根是真的，MA/RSI/ATR 不該少一天）。

### 20. 「分位」與「量比」使用者一定會互相驗算

分位母體＝歷史每天的 N 日均量；量比分母＝近 N 日均量。**兩者不可互推**（今日縮量與 5 日
均量仍在歷史高檔可同時成立）。2026-08-11 使用者以 219,571÷648,800=0.34 推「應該 33 分位」，
而 v3.35 已改過一次標籤文字仍再被誤讀 → **兩個數字並列顯示**，不要只給一個再靠文字解釋。

---

### 21. 公開檔的「範例數字」曾是真數字（S-1 私有化做一半）

`config_private.py.example` 進公開版控，其 docstring 的 JSON schema 範例在 2026-07-06
S-1 私有化時**直接抄了真實防守數字**（觸發價／釋出量／加保後強平價），一年多來公開可讀，
2026-08-21 才清掉。**S-1 的威脅模型是「repo 是 public」，不是「config.py 這個檔」**——
搬走真值卻在隔壁檔案的註解裡留副本＝沒搬。

**禁令**：`.example`、README、CLAUDE.md、測試檔、commit message **一律只寫顯假值**
（99999/88888/77777）。結構可以真，數字不准真。測試要真值就 `from config_private import`
＋缺檔 skip（見 `tests/test_defense_ladder.py`）。改任何公開檔前先問「這個數字是不是部位」。

### 22. P4 重啟偵測曾掛在錯的觸發點上

`detect_mart_restart()` 原本只被 `notify_defense_line()` 呼叫，而後者只在價格跌破
`ALERT_PRICE_LOW` 才執行——**「要用防守階梯的那一刻，才發現階梯早就壞了」**。
2026-07-13 的對帳基線在 8/19–8/21 上漲後失效（兩台馬丁各重啟 6~9 輪），
價格從沒跌破警報價，偵測邏輯就從沒跑過，一個多月無人知曉，最後靠人工對帳發現。

2026-08-21 改由 `scripts/daily_line_notify.py::maybe_send_mart_restart_alert()` 每日驅動，
重啟後 24h 內告警（去重 key＝基線日＋已重啟名單，更新基線後可再告警）。
**通則：偵測器的觸發條件不可與「它要保護的那件事」同時成立**，否則等於沒有偵測。

## 受保護決策（改動需使用者裁定）

| 項目 | 規則 | 正本 |
|---|---|---|
| 防守通知數字 | 真實數字在 `config_private.py`（gitignored）或 Actions Secret `DEFENSE_CONFIG_JSON`，公開 `config.py` 只留載入邏輯（fail-loud）。**馬丁止盈重啟即整表作廢**（新最後加倉價＝新起始價×0.659，整表重算；每日由 `maybe_send_mart_restart_alert` 偵測告警）。防守為**條件式**：每階執行前看 `final_low`/`ensemble_low`。**`ALERT_PRICE_LOW` 自 2026-08-21 起與第 1 階解耦**（獨立預警價，判準 `>=` 不再是 `==`）| vault「1b 1 BTC ROAD.md」；驗算見「1b 馬丁格爾數學稽核」；`_governance\STRESS-btc-three-tracks.md` |
| 雙幣回測 | **舊曲線與據其做的結論全部作廢**（權利金曾在結算日才定價，全史 +1733%→−90%）。**雙幣加碼決策不可依據此回測模組** —— 實際用法是梯形建議＋偏保守權重。殘留已知偏差：σ 用 ATR/close proxy 高估 ~1.6×（方向已知、接受） | `calculate_ladder_strategy` docstring（2026-06-17 拍板） |
| 四季論引擎 | `SEASON_ENGINE` 維持 `"v1"`，**切換 v2 需使用者裁定**。回放 2,992 天後不建議切換：v2 十二象限表對深熊嚴重度分級有結構性缺口。**不可回頭調參數讓驗收準則「看起來過」** | `Github\Cow\season_v2_replay_findings.md`；設計正本 `Github\Cow\season_v2_design.md`（皆在 vault） |
