# CLAUDE.md — Cow（BTC 投資戰情室）

**路徑：** `D:\Users\63191\Documents\GitHub\Cow`
**目前版本：** v3.34
**Live App：** https://mfyyo9qf5mymsrouxkfdgj.streamlit.app
**Streamlit 版本：** 1.37.1

---

## 執行指令

```bash
# 本地開發（必須用 Anaconda Python）
D:\Users\63191\AppData\Local\anaconda3\python.exe -m streamlit run app.py

# 增量更新 BTC K 線並推送 GitHub
D:\Users\63191\AppData\Local\anaconda3\python.exe collector/btc_price_collector.py --push
```

---

## 架構分層

```
app.py           入口點（不含業務邏輯；今日大盤速覽以 @st.fragment(run_every=60) 自動更新）
config.py        集中設定（均線週期、交易成本、倉位風控、SSL_VERIFY、WALK_FORWARD_EXIT_MODES）
data_manager.py  根層級數據管理器（TVL/穩定幣/資金費率歷史 SQLite 快取、指數退避重試）

core/            純函數層（技術指標、熊市底部評分、四季目標價預測）
service/         資料取得層
                 歷史：本地DB → Yahoo → Binance → Kraken → CryptoCompare（五層）
                 即時：Binance → Kraken → 本地DB（三層）
                 資金費率：Binance fapi → Bybit → OKX（三層）
                 宏觀：FRED CSV → Yahoo → FRED 備援 → 靜態 _FALLBACK（四層）
                 台股籌碼/估值：tw_chip.get_chip_bundle（TWSE 全量檔每小時快取 + TDCC CSRF 爬法）
strategy/        策略引擎（波段 Antigravity v4.1、Walk-Forward 回測、雙幣 Black-Scholes、推播）
handler/         Streamlit UI 各 Tab 實作
collector/       BTC 15m K 線收集器（年度分割 SQLite，Binance + Kraken 雙源）
scripts/         GitHub Actions 推播腳本 + Flex Message 除錯工具 + 回測驗證腳本
tests/           單元測試（bear_bottom、dual_invest、market_data）
db/              btcusdt_15m_YYYY.db（年度分割，雲端直接讀 repo 內 db）
```

---

## 最低價綜合評估（四季論底部優化，2026-06-04 新增）

**單一真實來源 `core/bottom_floors.py` → `compute_all_bottom_estimates()`**，LINE 推播與
dashboard（tab D2.5）**共用同一函式**，杜絕兩邊算法漂移。整合：

- **四季論趨勢底**（`season_forecast.project_bear_bottom`，v1.4）：bottom_mult 改「週期趨勢
  外插」(`extrapolate_bottom_mult`) 取代舊 median/p25。三輪 0.131→0.157→0.225 單調遞增
  （底部漸淺），舊 median 把當前輪預測過深；線性迴歸外插（留一法誤差 -19% 優於 median -30%）。
  `project_bear_bottom` 為 forecast_price 熊市分支與 bottom_floors 的**共用底部來源**。
- **4 個 floor**：200 週均線 / 冪律下界 / **礦工電費（硬地板）** / 礦工 all-in（警示線）
- **on-chain 錨**：Realized Price / Balanced Price / CVDD（`service/bottom_metrics.py`，
  資料源 bitcoin-data.com）
- **技術錨**：Mayer 底（SMA730×0.6）/ AHR999 抄底頂（AHR999=0.45 對應價）
- **final_low = max(四季論趨勢底, 礦工電費硬地板)**；ensemble = 強錨中位數

**礦工成本模型 `core/miner_cost.py`**（純數學）：
`電費盈虧 = hashrate_ths × eff_jth(date)/1000 × 24 × rate / btc_per_day(date)`；
btc_per_day 依減半切換（3600→1800→900→450）、eff_jth 分段 era 插值（**最大不確定來源**）、
all-in = 電費 × 1.6。rate＝電價，**2026-06 對齊 Cambridge CBECI 改 0.05**（舊 0.055；
config.MINER_ELECTRICITY_RATE 單一來源；eff_jth anchors 因缺實際 CBECI 硬體籃資料維持不動）。

**回測關鍵發現（2015/2018/2022 三輪熊底，`tests/bottom_floors_backtest.py`）**：
- **熊底/純電費 = 2.18→1.21→1.17x**（rate=0.05；收斂、**從未跌破** → 電費=硬地板；舊 0.055 為 1.98→1.10→1.06x）
- **熊底/all-in = 1.36/0.76/0.73x**（2018/2022 牛末仍跌破 all-in 至 ~0.73×；舊 0.055 為 ~0.67×）
- 2022 熊底 $15,476 落在 Balanced($11.4k)~Realized($20.5k) 之間
- 現況（2026-06，rate=0.05）礦工電費硬地板 ≈ $61.6k（隨算力動態；舊 0.055 約 $67.8k）

### 已知陷阱（本系統）
- **bitcoin-data.com 429 burst 限流**：連續 ~6 次請求即冷卻數分鐘。`service/bottom_metrics.py`
  端點間隔 4s + 遇 429 長退避（20s×n）+ **12h 持久化 json 快取**（`db/bottom_metrics_cache.json`、
  `db/hashrate_history.json`）。**勿密集探測**，會觸發長時間限流。
- on-chain 指標**僅約 4 年歷史**（bitcoin-data.com 免費層），只能驗證 2022 輪；2015/2018 靠礦工成本回測。
- mvrv-zscore 端點末筆偶為 nan（次要指標，已 graceful 過濾）。
- `compute_all_bottom_estimates(now=...)` 須傳 **naive datetime**（內部已 strip tz；但傳 tz-aware
  進 `project_bear_bottom`/`get_current_season` 會與 naive HALVING_DATES 比較拋錯——已在 bottom_floors 統一去 tz）。

---

## 相對高/低點雷達（逃頂 + 抄底，2026-06-08~09）

**單一真實來源**：`core/relative_high.py`（逃頂五維）＋ `core/relative_low.py`（抄底六維）＋
`core/trend_direction.py`（趨勢方向四維，第三軸），dashboard、BTC_WATCH.py（**正本在本 repo
根目錄**，2026-06-10 起不再維護 Crypto repo 那份）、LINE 推播共用，杜絕兩邊閾值漂移。

- **逃頂五維（30/25/26/15/10，理論總和 106 clamp 100；onchain 因 2026-07 併入 MVRV-Z 由 20→26）**：
  合約過熱 / 技術衰竭 / 鏈上派發 / 情緒過熱 / 總經逆風。`compute_escape_top_score` + `escape_top_meta`。
  詳細權重以 `core/relative_high.py::WEIGHTS` 為單一真實來源，此處僅摘要、勿手動精算複寫。
- **⚠️ 仍 NOT VERIFIED（資料不足，非網路）— OI×Funding 假頂折減**：`relative_high._score_derivatives`
  2026-07 新增「funding 過熱(年化≥30% → f_s≥14)但 OI 分位<70(去槓桿/未confirm)」時 funding
  貢獻 ×0.75 的假頂折減（**從不灌分、OI 無資料不折減**，只下修假頂、保守可逆）。
  → **2026-07-02 家用網路已實跑 `tests/relative_high_backtest.py`（跑得動、非被擋）**，但
  **funding 歷史僅回溯 2026-04-27 → 落在資金費率時代的相對高點只有 1 筆可擬合**，train 頂 0/test 頂 1，
  所有 AUC 退化（train nan、test 0.5/1.0，無統計意義），OI 亦因歷史不足未納入擬合。**「交互優於相加」
  無法驗證，瓶頸是 OI/funding 樣本太少、家用網路也翻不了。** 決策：**維持折減段現狀（保守可逆、不灌分）
  ＋保留待驗證標記，不移除**；等 funding/OI 累積更多相對高點樣本再重跑，屆時 AUC 未退步才把 synergy 轉正。
- **抄底六維（25/20/20/15/16/10，理論總和 106 clamp 100；onchain 因 2026-07 併入 MVRV-Z 由 10→16）**：
  長週期深跌 / 合約超冷 / 技術回穩 / 情緒恐慌 / 鏈上吸籌 / 總經順風。
  `compute_relative_low_score` + `relative_low_meta`。同上，權重以 `WEIGHTS_LOW` 為準。
- **權重由敏感度測試決定**（`tests/relative_high_backtest.py` / `relative_low_backtest.py`，
  鏡像方法：swing 高/低點 + 其後 60 日反向 18% 為正樣本、時序 train/test、Mann-Whitney U AUC、
  grid search）。**樣本少 → grid 必過擬合 → 採專家配重 + 單維 AUC 排序微調**。
- **兩側天生非對稱**：底部最強維度是「長週期深跌」(AUC 0.662)，頂部是「合約過熱」——
  底部靠估值便宜、頂部靠槓桿過熱。這不是設計缺陷，是市場結構。
- **趨勢方向第三軸（2026-06-09）**：逃頂/抄底是「貴不貴」相對估值量表，`trend_direction`
  補正交的「風往哪吹」：均線結構±40 / MACD±30 / 斜率±15 / ADX±15 → 有號淨分 [-100,+100]。
  ADX<20 時方向三維打 0.6 折（盤整防假突破）。可同時「強多頭＋逃頂高」或「空頭＋抄底高」
  （勿純憑估值接刀），三軸合看。LINE Flex 顯示在波段雷達 box 頂部橫幅。
- **維度狀態三分類**（抄底側 `core/relative_low`）：`UNFITTED_DIMS_LOW`（待累積後可回測，如 OI）、
  `RULE_BASED_DIMS_LOW`（規則式不可統計擬合）、`PENDING_FIT_SUBDIMS_LOW`（可擬合但缺歷史源）。
  - onchain：2026-06 敏感度驗證通過，已不在任一清單（SOPR 單維 AUC 0.585、加入合成無害且隨權重
    單調有益，見 `tests/relative_low_backtest.py::validate_unfitted_dims`）。ETF 子項 2024+ 資料薄沿用專家權重。
  - macro **拆兩子維**：event-window（事件臨近）＝規則式、永久不可擬合 → `RULE_BASED_DIMS_LOW`；
    dovish flags（通膨/就業）2026-07 已用 FRED 回測完成，`PENDING_FIT_SUBDIMS_LOW` 清空、改為
    `WEAK_SUBDIMS_LOW`（結論見下方「macro dovish/hawkish flags FRED 回測結論」節，單一真實來源，
    不在此重複）。UI 以〔規則式〕（藍）/〔未擬合〕（橘）兩種 tag 區分。
  - 逃頂側 `UNFITTED_DIMS=("onchain",)` 不變。

### macro dovish/hawkish flags FRED 回測結論（2026-07，`tests/relative_low_macro_backtest.py`）
用 FRED CPIAUCSL/PCEPI/PAYEMS/UNRATE 建 **point-in-time**（每月觀測掛 observation_date+發布延遲
為 available_date，評估日只取已公布者 → 無前視）dovish/hawkish flag 序列，對 swing 轉折±18% 樣本測單維 AUC：
- **抄底 dovish（通膨降溫＋就業轉弱）＝弱/落後確認**：全期 AUC **0.448**（方向反，底部觸發率 53%＜非底 74%）、
  資金費率時代 0.562（弱）；增量在實際 macro 權重(λ≈0.07)下 Δ≈+0.02 可忽略。
  → **底部領先 macro 改善**（落底時 Fed 尚未轉鴿、通膨/就業確認未到），dovish 是落後確認非領先訊號
  → 不給實證權重，維持低權規則式灰燈（與抄底「估值便宜才是最強底部維」一致，macro 非底部驅動力）。
- **逃頂 hawkish（通膨升溫＋就業強勁）＝方向明確有效**：全期 AUC **0.607**、資金費率時代 **0.660**
  （頂部觸發率 67%＞非頂 47%）→ 頂部與升息環境（通膨熱+就業強→Fed 抽流動性→BTC 逆風）同步，支持沿用權重。
- **再添一筆頂底非對稱證據**：頂部靠「升息環境」可被 macro 標記、底部 macro 無領先力（靠估值/槓桿清洗）。

**BTC_WATCH.py 共用**：純幣安環境連 fapi/dapi + path import 算分。OI 用
`openInterestHist`（5m×13 滾動清洗 + 1d×30 分位）取代失效的相鄰 60s 差值；防線用
`bottom_floors.final_low`（fallback 54000）。**外部維度（2026-06-16 補）**：隨日線每小時
刷新一次抓 ETF（`get_etf_flow_summary` 讀 committed `etf_flow.json`，Farside 403 備援）、
SOPR（`get_latest_bottom_metrics`，bitcoin-data 12h 快取）、BTC.D 趨勢（`get_btcd_trend`，
本地 OI 快照）、總經事件（`get_next_macro_event`，本地 `macro_events.json`，**不打被擋的 FRED**）。
可得天花板升至 **逃頂/抄底各 99**（唯缺 macro 通膨/就業 dovish/hawkish flags 需 FRED；2026-07-03
onchain 併入 MVRV-Z 後 WEIGHTS 原始總和變 106 會被 clamp(100) 蓋掉超編部分，「缺 FRED」實際只再
少 1 分，不是舊版算的 7 分——`BTC_WATCH.TOP_CAP`/`LOW_CAP` 已同步更新為 99，勿再用「100-7」捷徑算）。

**⚠️ 陷阱：Farside ETF 佔位 0.0（2026-07 修）**：Farside 對「最新未定案日」回 `0.0`，若當真實值
存入 `db/etf_flow.json`，會被 streak 邏輯當成非流出 → `consecutive_outflow_days` 歸 0、latest 誤標
中性；實測曾把「連續 8 天機構流出」顯示成「🟢 淨流入」（完全反向、遮蔽逃頂派發訊號）。修法：
`service/etf_flow._parse_html_to_daily` 不寫入 `val==0.0`、純函數 `_summarize` 過濾所有 0.0（見
`tests/test_etf_flow.py`）。**交易日淨流量恰為 0.0 極罕見，一律視為當日無資料。**

詳見 PLAN：`Obsidian/Github/Cow/20260608plan_相對高點判斷.md`、`20260609plan_相對底部判斷.md`。

### MVRV-Z / Hash Ribbons 社群參考訊號回測結論（2026-07，`tests/relative_ref_signals_backtest.py`）
原本 `reference_top_signals`/`reference_low_signals` 的 MVRV-Z、Hash Ribbons 只是「顯示不計分」，
用本地已快取歷史（`db/bottom_metrics_cache.json` 的 mvrv_zscore 2022-07+、`db/hashrate_history.json`
全史，零新網路請求）跑 swing 高低點 AUC 驗證（方法同 relative_high/low_backtest.py）：
- **MVRV-Z 逃頂**：swing 高點 n_pos=20/n_neg=53，值越高越像頂 → **AUC=0.592**，過 0.55 門檻。
- **MVRV-Z 抄底**：swing 低點 n_pos=23/n_neg=104，用 -z 當單調子分數 → **AUC=0.732**，
  **比現役 SOPR(0.585) 還強**，過門檻。
- **Hash Ribbons 抄底**（投降強度 (SMA60-SMA30)/SMA60）：n=191 → **AUC=0.359，方向反/無效**。
  不代表理論錯誤，可能是「持續投降深度」非最佳代理（黃金交叉事件本身未另測，Hash Ribbons 官方
  定義的最強訊號其實是交叉瞬間，不是投降期間的持續深度）。**2026-07 已整段移除**：原本以「參考
  顯示不計分」保留在 watcher 面板，但顯示的「🟡 礦工投降中→打底醞釀」本身就是那個被證明方向相反的
  訊號（無中性事實價值、易誤讀成偏多），故連同 `core/relative_low._hash_ribbon_read`/
  `reference_low_signals` 與 `BTC_WATCH._ref_rows` 一併刪除。**勿再加回**（否決理由見 relative_low.py 檔頭）。

**落地細節**：MVRV-Z 從「參考顯示」轉正式計分後（權重見上方逃頂/抄底維度摘要，onchain
20→26／10→16），`compute_escape_top_score`/`compute_relative_low_score` 新增 `mvrv_z` 參數，
三個消費端（`BTC_WATCH.py`、`handler/tab_macro_compass.py::_gather_radar_externals`、
`scripts/daily_line_notify.py::_compute_radars`）皆已補上 `mvrv_z=ext.get("mvrv_z")`／
`bm.get("mvrv_zscore")` 餵入。`reference_top_signals`（原本只含 mvrv_z）與 `reference_low_signals`
（拿掉 mvrv_z 後只剩 Hash Ribbons）皆已整支移除，雷達不再有任何「參考顯示不計分」的社群訊號。

---

## 台股/美股版逃頂/抄底（watcher 股票分支，2026-06-21 起）

加密雷達的 funding/OI/鏈上維度股票無對應 → 台股改用**籌碼/估值**替代；美股缺免費籌碼源，
改用**純 OHLCV 通用軸**（量價背離+結構轉折，複用 `core/divergence`）。

- **`core/relative_high_tw.py`（v0.5）／`relative_low_tw.py`（v0.4）**：現行配重（2026-07-02 拍板）
  ＝**逃頂**：技術30/估值30/量能18/槓桿10/法人4/散戶(TDCC)8＋vol_price 8 疊加（核心 100＋疊加，
  clamp 100）；**抄底**：槓桿40/技術30/法人20/估值10（四維恰 100）。技術維度**複用 core/divergence**
  （與加密同源）、法人買賣超以**近20日均量正規化**。`compute_relative_high_tw/low_tw + *_meta`。
- **`core/relative_high_us.py`／`relative_low_us.py`（v0.1）**：技術背離50（複用
  `relative_high_tw._score_technical_high`，市場無關）+ 量價背離30 + 結構轉折20，權重為專家經驗值
  ——**2026-07-02 家用網路 50 檔 10 年回測：三維全近雜訊（AUC 0.47-0.51），無實證背書，維持現狀
  但標「僅參考、未獲實證」**（權值股長多、真頂樣本僅占 9%，純技術面在 mega-cap 上抓頂近乎不可能，
  跟台股/加密「頂靠估值貴」一致，缺籌碼/估值維時雷達本質偏弱）。踩坑：`us_universal_backtest._fill`
  原用 `reindex` 對齊分數，跨 50 檔 concat 後 DatetimeIndex 重複日期會崩，已改逐檔 positional 賦值。
- **`service/tw_chip.py`**：`get_chip_bundle(symbol, yyyymmdd, lookback=7)` → {margin,
  institutional, valuation, tdcc, **as_of**}，每源 best-effort（抓不到回 None → 評分灰燈不 crash）。
- **`core/relative_universal.py`**（純 OHLCV，台股/美股共用）：`score_volume_price_top/bottom`
  （量增價縮＝出貨／量縮價增＝賣壓竭盡，近5/20日均量比+近5日報酬變化）、`score_structure_top/bottom`
  （前高未過／前低未破，複用 `core.divergence.detect_swing_structure`）、`rescale_dim(sig, new_max)`
  （子維分數按比例縮放到新配額，供疊加進其他框架用）。
- **三軸 composite 共用 `action_ensemble.compute_composite_action`**：台股**不傳 cycle_score**
  （估值對底部是雜訊、max 僅 10 達不到 `CYCLE_DEEP_VALUE=22` 門檻），改由重配重後的 low_score≥60
  驅動（已含「融資清洗」權重40 這個校準最強底部維）。

### 校準沿革（結論導向；逐輪 AUC 明細見下方 Obsidian PLAN）
- **v0.2（2026-06-23）拍板**：逃頂靠估值貴（PE/PB 絕對值 AUC 0.627/0.640 → 估值最強維，絕對值
  勝個股分位 0.452——分位會把「本質就貴/便宜」洗成中性）；抄底靠融資清洗（斷頭 AUC 0.564 → 槓桿
  最強維，估值反是雜訊/價值陷阱，AUC 0.45）——**台股頂底與加密非對稱**（頂部估值貴、底部融資
  清洗；加密底部反而靠估值便宜）。TDCC major_pct 週變化 delta／連增週數重測皆未優於靜態 level
  （`scripts/tw_tdcc_retest.py`），當時維持低權、不動評分。
- **v0.3/v0.4/v0.5（2026-07-02）全市場 2080 檔 swing 回測拍板**（`scripts/tw_universal_backtest.py`，
  out-of-sample≥2024，公司網路本地 climber DB 可跑）：逃頂 vol_price AUC 0.566 轉正式（5→8）、
  structure 0.483 雜訊移除；抄底 vol_price/structure 皆雜訊移除、**TDCC 大戶 major_pct AUC 0.422
  （方向反、比亂猜差，確認舊測的弱維懷疑）→ 整維移除**（原逃頂/抄底皆保留低權，此輪抄底側改為
  完全移除），釋出的 15 分重配給抄底最強兩維：槓桿清洗 30→40、技術回穩 25→30。移除反指標/雜訊維
  同 Hash Ribbons 邏輯——不留計分。**頂底非對稱再證**：量價背離是頂部訊號（逃頂有效、抄底雜訊）。
- 詳見 PLAN：`Obsidian/Github/Cow/20260621plan_股票版逃頂抄底維度.md`、
  `20260622plan_台股維度回測校準.md`、`20260626plan_台股維度v0.3弱維強化.md`、
  `20260702plan_通用量價結構訊號與美股框架.md`。

### 台股資料源踩坑
- **TWSE 端點都是「市場全量單日檔」非個股查詢**：抓整檔每小時快取再 filter symbol。融資融券
  `marginTrading/MI_MARGN`(selectType=STOCK)、三大法人 `fund/T86`(ALLBUT0999)、本益比PB
  `afterTrading/BWIBBU_d`(ALL)。回應有時包在 `tables[]` → 需深找含 `fields+data` 的 table。
- **Accept-Encoding 勿帶 br**：T86 回 brotli，requests 無 brotli 套件時解碼壞 → 固定
  `gzip, deflate`。
- **MI_MARGN 單一回應即含「前日＋今日餘額」** → 融資變化免多日累積；T86 為單日買賣超。
- **TDCC 集保大戶分布要 GET→POST CSRF**（鏡像 tw_stock_climber）：先 GET `qryStock` 抓
  `SYNCHRONIZER_TOKEN`/`SYNCHRONIZER_URI`，再 POST 帶 token + firDate/scaDate（最近已公布週五，
  扣 7 天公布延遲）。`pd.read_html` 解析持股分級表，大戶≥1000張/中實戶≥400張/散戶≤50張。
  **同週同檔記憶體快取不重抓；勿密集打**（每檔間需 sleep）。**現況**：逃頂側散戶 retail_pct
  仍低權保留（AUC 0.537，樣本 2023-09 起 2.7 年薄）；**抄底側大戶 major_pct 已於 2026-07-02
  因 AUC=0.422 方向反整維移除**（見上方校準沿革），非「維持低權」。
- **上櫃籌碼四維 TPEx fallback（2026-06-23）**：三個日檔（估值/融資/法人）皆「上市 TWSE → 上市查無
  （上櫃股）轉打對應 TPEx 端點」，`_fetch_market_file` 加 `base` 參數（預設 _TWSE、上櫃傳
  _TPEX="https://www.tpex.org.tw"；**cache key 改 (base, endpoint, date)** 避同端點撞檔；TPEx 日期皆
  `_tpex_date` 轉 yyyy/mm/dd）：
  - 估值 `_get_valuation_tpex`：TPEx `www/zh-tw/afterTrading/peQryDate`，欄序 PE=2／殖利率=5／PB=6、
    **無收盤價欄 → close=None**（呼叫端勿對 close 做算術）。
  - 融資 `_get_margin_tpex`：TPEx `www/zh-tw/margin/balance`，欄序 2前資餘額/6資餘額/10前券/14券餘額
    （單位張同 TWSE）；TWSE/TPEx 共用 `_margin_dict` 把前日/今日餘額轉 dict。
  - 法人 `_get_institutional_tpex`：TPEx `www/zh-tw/insti/dailyTrade`(type=Daily)，欄序 4外資(不含自營)/
    13投信/22自營合計/23三大法人合計（與 TWSE T86 的 4/10/11/18 不同 → 兩函式分開），評分只用 total_net。
  → 上櫃股（6488/8069）OHLC + 籌碼四維（融資/法人/估值/大戶）全部可用，逃頂/抄底不再因上櫃灰燈
  （6488 抄底 15→38）；上市路徑不變。
- **台股 `.TW`→`.TWO` 上櫃備援（日線與即時報價需各自套用，勿漏放一邊）**：`fetch_ohlc`（日線）
  台股 `.TW` 查無自動改試 `.TWO`（上櫃 Yahoo 後綴，classify 無法離線分上市/上櫃；上市查到即
  break 不浪費第二請求、全失敗 `raise ... from last_err`）。`fetch_live_quote`（即時報價/成交量）
  **原本漏了同一段備援**，導致所有上櫃股的現價/成交量永遠 404（非網路波動，同一 symbol 連續多次
  皆 404，`.TWO` 立即成功）；2026-07-03 已抽出共用 `_tw_candidates()` 補上，日後新增/修改台股
  相關 fetch 函式務必套用同一份候選清單，不要各自複製判斷。
- **TWSE 日檔是 EOD（盤後）公布 → 必須 walk-back**：`get_chip_bundle` 呼叫端常傳「今日」（watcher
  用 Yahoo 最後日線日期，盤中可能是未收的今天），但今日 EOD 檔尚未出、連假（如 2026-06-19 端午）
  整週無檔 → 三日檔會**整片 None**。修法：先用**單一探針**（BWIBBU 市場檔非空判斷）從 date 往前找
  「最近已公布交易日」（最多 lookback=7 天、跳過週末/未公布日），三源對齊同一 `as_of` 再抓
  （BWIBBU 已快取）。**用單一探針而非多源×多日盲掃**，避免撞 TWSE 限流。watcher note 顯示
  「籌碼資料截至 {as_of}」。
- **台股/TPEx 盤中即時報價法定延遲 ~20 分鐘，勿用 timestamp 新舊判斷盤中/已收盤**：
  `service/ohlc_universal.live_quote_freshness` 原本用「Yahoo `regularMarketTime` 距今 <15 分鐘」
  猜是否盤中，這對美股成立、對台股不成立——台股盤中 age 幾乎必然 >15 分鐘（法定延遲），會被
  永遠誤判成「已收盤」。改用當下是否為交易時段（`_is_tw_trading_hours()`，週一~五 09:00-13:30
  台北時間）判斷，如實顯示「盤中（資料延遲）」而非「已收盤」。同理新增 `is_daily_bar_forming()`
  判斷日線最後一根是否為「今日進行式」（避免「最新日線」跟「現價」顯示重複）；`resolve_live_volume()`
  處理即時成交量偶發缺漏的閃爍問題（退回快取值，逾 2 個刷新週期才附註快取時間）。
- Cow **不 import tw_stock_climber**（保持自包含、雲端可跑），僅鏡像爬法/分級概念。

**即時成交量 + 台股週轉率**：`service/ohlc_universal.fetch_live_quote` 新增 `volume`
（Yahoo v8 chart `meta.regularMarketVolume`，同一次請求內、零額外網路成本，台股/美股皆有）。
台股週轉率＝即時成交量÷已發行股數，股本資料來源 `service/tw_chip.get_shares_outstanding`：
- TWSE OpenAPI `https://openapi.twse.com.tw/v1/opendata/t187ap03_L`（上市，無需 date 參數/驗證，
  已用 2330 實測：259.32 億股，與已知量級吻合）。
- 上市查無（上櫃股）→ TPEx OpenAPI `mopsfin_t187ap03_O` fallback（欄位英文命名，
  `SecuritiesCompanyCode`/`IssueShares`，已用 6488 驗證）。
- **這個 domain 的 requests 自動編碼偵測常猜錯** → 強制 `r.encoding = "utf-8"`（`.json()` 內部
  邏輯 `if not self.encoding` 才觸發 BOM 猜測，設了就走 `.text` 走指定編碼，故此設定確實有效）。
- 回應體積大（~1MB+全市場），實測 20–45 秒，故 **timeout 拉到 60s、快取拉到 24h**（不比照其他
  籌碼檔每小時重抓，股本變動極不頻繁）；抓取失敗退回舊快取而非清空。
- **✅ 已驗證（2026-07-02 家用網路實打正式 API）**：`watcher.py` 進 2330，`get_shares_outstanding`
  本體實連 TWSE OpenAPI 回 **25,932,370,067 股（≈259.3 億股，量級正確）**，週轉率
  29,457,313÷股本×100 = **0.11%**（<0.5% 合理）。

**已知陷阱**：watcher.py 的 `_panel(result, meta_fn, cap, name, dims)` 呼叫端 **`dims` tuple 必須
與 `compute_relative_*` 回傳 signals dict 的 key 完全一致**（新增維度時容易忘記同步更新呼叫端的
dims tuple——曾抓到台股逃頂/抄底面板漏了 `vol_price`/`structure`，分數已算進總分但完全不會顯示
在面板列表，使用者無從判讀分數從哪來）。改 `compute_relative_*` 的 signals keys 後，**務必 grep
所有 `_panel(...)` 呼叫端**確認 dims tuple 同步更新。

---

## 已知陷阱

> 功能專屬踩坑記錄在對應章節內（見「最低價綜合評估」「台股/美股版逃頂/抄底」），此處為
> 跨功能通用陷阱，不重複列。

### 1. `@st.fragment` 靜默失效（現價停止自動更新）

根因：`@st.fragment(run_every=60)` 傳入 DataFrame/Series 時序列化失敗，fragment 停止重跑但不報錯。

```python
# ❌ 傳 DataFrame/Series 會靜默失效
@st.fragment(run_every=60)
def render(btc, curr): ...

# ✅ 只傳 float scalar
@st.fragment(run_every=60)
def render(prev_close: float, rsi14: float, ...): ...
render(
    prev_close=float(btc['close'].iloc[-2]),
    rsi14=float(curr['RSI_14']) if 'RSI_14' in curr.index else 50.0,
)
```

### 2. `@st.cache_data(ttl=60)` + `run_every=60` 衝突

根因：fragment 60 秒重跑，TTL 也 60 秒 → 永遠命中快取 → 數據不刷新。
修法：`fetch_realtime_data()` 不掛 `@st.cache_data`。

### 3. `.get(key, default)` 對顯式 `None` 值不觸發預設（dict 與 pandas Series 皆同）

`data = {'source': None}`；`data.get('source', '模擬值')` → 回傳 `None`，不是 `'模擬值'`。
`curr.get('RSI_14', 50)` 在 Series 上同樣不可靠。
修法：`data.get('source') or '模擬值'`；`float(curr['RSI_14']) if 'RSI_14' in curr.index else 50.0`

### 4. NaN 判斷

避免 `val == val`。改用 `import math; not math.isnan(val)`。

### 5. 企業防火牆封鎖 Binance

確認方式：看 fragment 內「數據更新時間」是否每分鐘更新。
- 有更新 = fragment 正常，是 API 連線問題
- 沒更新 = fragment 本身沒跑

即時價格備援鏈（已實作）：Binance 現貨 → Kraken Ticker → 本地 15m DB 最新一筆

```
Kraken：GET https://api.kraken.com/0/public/Ticker?pair=XBTUSD
取 result['XXBTZUSD']['c'][0]
```

### 6. service 層來源追蹤慣例

`fetch_realtime_data()` 回傳 dict 含 `price_source`、`funding_rate_source`、`tvl_source`。
- UI 層（app.py）直接讀 `rt.get('price_source', '歷史收盤')`
- **不在 UI 層做 `rt['field'] is not None` 判斷**（leaky abstraction）
- `get_latest_local_price()`：不帶 `@st.cache_data`，供即時備援
- `read_btc_15m()`：有 `ttl=86400` 快取，**不可**用於即時價格

### 7. `reindex(method='nearest')` 早於資料起點填充定值

```python
# fund_hist 從 2021 年起，chart_df 從 2015 年起
fund_sub = fund_hist.reindex(chart_df.index, method='nearest')
# 2021 年前會填成第一筆值（常數線）→ 手動清除
fund_sub.loc[fund_sub.index < fund_hist.index[0]] = np.nan
```

### 8. 資金費率即時備援鏈

| 來源 | URL | 欄位 |
|------|-----|------|
| Binance fapi | `fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT` | `lastFundingRate` × 100 |
| Bybit | `api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT` | `result.list[0].fundingRate` × 100 |
| OKX | `www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` | `data[0].fundingRate` × 100 |

### 9. 圖表欄位名稱與顯示標籤混淆

欄位名 `EMA_20`（含底線）直接用在圖例易被誤讀為 `SMA 20`。
修法：`_ma_label(col)` helper → `col.replace("_", " ")` → `EMA 20` / `SMA 50`。
當 `exit_ma_key == 'EMA_20'` 時，進場線與防守線同一條，合併標籤：`"EMA 20 (進場 ＆ 防守線)"`。

### 10. AHR999 冪律公式錯誤（舊版膨脹至 $177 萬）

根因：舊版用線性指數模型 `10^(2.68 + 0.00057×days)`，2026 年後估值嚴重膨脹。
修法：改用 Giovanni Santostasi 冪律模型：

```python
# ✅ 正確
estimated_price = 10 ** (-17.01467 + 5.84 * np.log10(days_since_genesis))
# ❌ 舊版（勿用）
estimated_price = 10 ** (2.68 + 0.00057 * days_since_genesis)
```

### 11. Walk-Forward 雙重移位 Bug

根因：`bull_trend = close > sma200` 用了 `close_shifted`（前一日收盤），整體再 `shift(1)` → 實際用到 2 天前的資料。
修法：所有條件統一使用**當日值**，最後**一次性** `shift(1)`。

```python
# ✅ 正確：統一用當日值，最後一次 shift
bull_trend = close_vals > sma200_vals       # 當日
is_entry   = bull_trend & rsi_bull & ...
entry_mask = pd.Series(is_entry).shift(1).fillna(False).values  # 一次移位

# ❌ 錯誤：中途用 shift，再統一 shift → 雙重移位
bull_trend = close_shifted > sma200_vals    # 前日收盤
is_entry   = bull_trend & ...
entry_mask = is_entry.shift(1)              # 已是 2 天前資料
```

### 12. Walk-Forward 進場乖離硬編碼 1.5% 上限

根因：`dist_pct <= 1.5` 硬編碼造成極少進場（ROI -22% vs swing +1654%），與 swing.py 行為不一致。
修法：改為可選參數 `entry_dist_max_pct`（預設 `None` = 無上限）；UI 提供滑桿，設為 0 = 不限。

### 13. 派網 Bot API 不支援幣本位網格與馬丁格爾

派網官方 Bot Open API（`/api/v1/bot/orders`）的 `buOrderTypes` 只有三種：
`futures_grid`（U本位期貨網格）、`spot_grid`（現貨網格）、`smart_copy`（智能跟單）。

**幣本位合約網格做多** 與 **馬丁格爾機器人** 均不在清單內，且 App 手動建立的機器人也不會出現在此 API 回傳結果。帳戶餘額 API 同樣不含機器人內的資產。

→ 派網 API Key 對本專案目前無用，勿再嘗試讀取機器人狀態。

### 14. GitHub Actions 用量：Cow 為公開 repo，不計入免費額度

GitHub Actions 免費方案：私有 repo 2,000 分鐘/月，**公開 repo 無限制**。

Cow 是公開 repo，`price_alert.yml`（每小時）與 `daily_line_notify.yml`（每天 3 次：台灣 08:23 / 13:39 / 18:27）均不消耗配額。
目前唯一消耗私有配額的是 `Notion_auto`（約 150 分鐘/月），距上限 2,000 分鐘仍有大量餘裕。

### 15. Gemini 2.5 系列為 reasoning 模型，預設 thinking 吃光 output token

根因：`gemini-2.5-flash` 預設開啟「思考」，會把 `maxOutputTokens` 消耗在 thinking 上，
導致 `candidates` 無實際文字輸出（症狀：呼叫耗時很久 + 回傳空）。翻譯/摘要不需思考。
修法：`generationConfig.thinkingConfig.thinkingBudget = 0` 關閉思考（見 `core/gemini_client.py`）。

### 16. Gemini ListModels 會列出已下架模型

`v1beta/models` 清單含 `gemini-2.0-flash`，但實際 `generateContent` 回 404
「This model is no longer available」。**不要以 ListModels 有列出就當可用**，需實打驗證。
目前可用：`gemini-2.5-flash`。

### 17. 新聞來源：Reddit 免認證被 IP 封鎖、X 免費 API 已關閉

- Reddit `hot.json` 對公司網路 IP 與雲端共享 IP 均回 403（IP 層級，換 User-Agent 無效），
  需 OAuth 才穩定 → 改用 **CoinGecko `/search/trending`** 當社群熱度指標（免金鑰）。
- X/Twitter 免費 API 已關閉，無穩定免費抓法 → 不納入。
- 新聞媒體源：CryptoCompare News + Cointelegraph/CoinDesk/Decrypt RSS，聚合去重（`service/news.py`）。

### 18. 新聞中文化的省 token 三層機制（`service/news_i18n.py`）

Gemini 翻譯成本與「Streamlit Cloud 休眠/喚醒」脫鉤的關鍵：
1. 批次：一次 prompt 處理整批（最多 8 則）回 JSON，避免逐則往返。
2. 持久化快取 `db/news_i18n.json`：以 url 為 key，翻過的**永不重翻**（記憶體 `@st.cache_data`
   在 cold start 會清空，故需落地檔案才能跨休眠續用）。
3. 總開關 `NEWS_I18N_ENABLED=false` 可完全停用翻譯（0 API 呼叫）。
   - 休眠時不執行 script → 0 token；真正成本 = 「新出現且沒翻過的新聞則數」。

### 19. requirements 鎖版本：勿替 numpy/pandas 加上限（pandas-ta 0.4.x 需 pandas>=2.3.2）

pandas-ta 在 PyPI 只有 pre-release 版（0.4.67b0 / 0.4.71b0），且**依賴 `pandas>=2.3.2`**；
新版早已無舊 0.3.x「`from numpy import NaN`」的 numpy<2 限制。

**踩坑（已造成線上部署中斷）**：曾誤判 pandas-ta 仍為需 numpy<2 的舊版，鎖了 `numpy<2` +
`pandas<2.3`，與 pandas-ta 0.4.x 的 `pandas>=2.3.2` 硬衝突 → Streamlit Cloud（Python 3.13
+ uv）報 `No solution`，pip fallback 後去 source-build pandas 2.2.2 卡死，app 起不來。
**原本「全無 pin」反而能正常 build**。

**正解**：
- `pandas-ta==0.4.71b0`（pin 確切 pre-release 版本，uv 才願意解析其 pre-release）
- `pandas>=2.3.2`、`numpy>=1.26`，**一律不加上限**
- 雲端 build 走 uv（Python 3.13），uv 預設擋 pre-release，唯「明確 == 該 pre-release 版本」放行
- 鎖版本前先看一次雲端 build log 確認現役版本，不可憑記憶臆測周邊套件需求

### 20. 同檔案裡有 `@st.cache_data` 裝飾器，import 整個檔案就會拖 streamlit 進來

`service/macro_data.py` 頂層 `import streamlit`（給其他函式的快取裝飾器用）+ `import yfinance`，
但 `get_next_macro_event()` 本身只讀本地 JSON，完全不需要這兩個重依賴。**只要 import 那個
檔案（即使只是為了呼叫一個不相關的函式）就會連帶 import streamlit**，公司網路環境下這個
import 動作本身會卡住逾時（實測 >10 秒無回應，且無 timeout 保護，try/except 攔不到「卡住」，
不是拋例外）。已抽出零重依賴的 `service/macro_events.py` 解決。**教訓**：一個檔案裡若混了
「給 dashboard 用、需要 streamlit/yfinance」與「給 CLI/排程用、零依賴」的函式，被 CLI 端
lazy import 拖累整包重依賴——新增純邏輯函式前，先看它要放的檔案頂層 import 了什麼。

### 21. 防守通知文案曾寫死過時計畫數字（2026-07-04 修，C-1；2026-07-06 S-1 數字私有化）

舊 `notify_defense_line` 寫死一組過時的防守計畫敘述（哪一階關哪台機器人、對應強平價），
與正確推移表不符（詳見 vault「1b 馬丁格爾數學稽核」的驗算過程）。
修法：推移表進 `config.DEFENSE_LADDER`（含各階觸發價/釋出 BTC/強平價/條件式附註），
文案由 `facade.build_defense_message` 動態組裝並依現價標 🔴/⚪；`ALERT_PRICE_LOW`
對齊第 1 階觸發價（C4 拍板，警報即行動訊號）。守門：`tests/test_defense_ladder.py`（5 項：
公式自洽/門檻=第1階/單調/文案完整/標記跟價）。**規則**：(a) 數字正本 = vault
「1b 1 BTC ROAD.md」；**2026-07-06 起真實數字改存 `config_private.py`（gitignored）
或 GitHub Actions Secret `DEFENSE_CONFIG_JSON`**，公開 `config.py` 只留載入邏輯
（fail-loud，缺失即 raise，見 `config_private.py.example`）——**計畫更新須同步
`config_private.py`，不再是公開 config**；(b) **馬丁止盈重啟即本表作廢**（新最後加倉價
= 新起始價 × 0.659，整表重算）；(c) 防守為**條件式**（2026-07-04 拍板）：每階執行前看
`final_low`/`ensemble_low`，第 3 階僅當模型熊底超過門檻才執行。
（2026-07-06 升級：`config.DEFENSE_DECISION_CARD` 四分區決策卡——第 1 階警報時做**一次性**
政策選擇，每階附註帶隱含押注門檻；依據 `_governance/STRESS-btc-three-tracks.md`。）

### 22. 雙幣回測曾在結算日才定價權利金（2026-07-06 修，C-7/C-8/C-9）

舊 `run_dual_investment_backtest` 三病：①權利金在**結算日**以 S=fixing 定價——被行權時
call/put 深入價內、price≈內在價值，行權損失被權利金「加回」→ 回測中被行權幾乎零成本；
②σ 年化用 `√(365×24)×0.5`（小時線因子套日線資料，σ 膨脹 ~2.45×）；③`calculate_bs_apy`
5% APY 地板進回測（每單保底）。修法：權利金於**開單時**鎖定（開單日 S、σ=`ATR/close×√365`、
`apy_floor=0.0` 參數化，live 顯示端維持 0.05 地板）；結算段只讀 `locked_yield`。
**修正後全史 Equity_BTC +1733% → −90%**——不是修壞：兩版交易路徑完全相同（622 筆結算、
行權事件同），差異全在收益率；−90% 揭露的是**策略結構問題**：2017-10 被行權轉 USDT 後
卡了 5 年（$5,430 出場、2022-08 $20,834 才抄回），結算樣本 95.8% 時間在 USDT 態，
牛市中以 BTC 計價自然崩——舊曲線靠灌水權利金蓋住這件事。
**規則**：(a) 舊曲線與任何據其做的結論作廢；(b) **雙幣加碼決策不可依據此回測模組**
（自動翻轉策略非實際用法；實際用法=梯形建議＋偏保守權重＋少被行權，見
`calculate_ladder_strategy` docstring 的 2026-06-17 拍板）；(c) 對照腳本
`tests/c7_compare_tmp.py`（本地）；(d) 殘留已知偏差：σ 用 ATR/close proxy 系統性
高估 ~1.6×（日內 range vs 日報酬 σ），現在回測/live 兩端**一致地**略高估
（finance-calc-reviewer 2026-07-06 覆核，方向已知、接受）。

### 23. 四季論 v2 十二象限狀態機已完整可用但預設不啟用（2026-07-06，B1）

`core/season_forecast.py` 新增 `_derive_market_axis`／`derive_effective_state`／
`_resolve_ath_ref_v2`（設計正本 `Github\Cow\season_v2_design.md`），`forecast_price`
加 `season_engine` 參數，預設讀 `config.SEASON_ENGINE="v1"`——**v1 路徑本次零改動**，
既有測試/呼叫端行為不受影響。**回放對照（2018-04~2026-07，2,992 天）後不建議
現在切換 v2**：三驗收準則僅「防抖降低切換次數」乾淨過，「差異集中度」與「2022
熊底一致性」未過，且發現 v2 十二象限表對深熊嚴重度分級有結構性缺口（v1 三層
-10/-20/-30% 門檻 vs v2 二元市場軸，972/1,379 差異天源於此，非設計文件命名的
目標格）。**規則**：(a) `SEASON_ENGINE` 維持 `"v1"`，切換需使用者裁定（受保護
設定）；(b) 不可回頭調十二象限表/防抖參數讓準則「看起來過」——那是事後合理化；
下次推進需先做 `season_v2_replay_findings.md` 列的人工檢視；(c) 消費端已加
`forecast_type="observe"` 防護（`tab_macro_compass.py`／`daily_line_notify.py`），
`v2` 若未來啟用不會因 target_* 為 None 崩潰；(d) 單元測試
`tests/core/test_season_v2.py`（24 項）＋回放腳本 `tests/season_v2_replay.py`
（本地）。

### 24. `_df_from_sqlite` 曾強制欄名全轉小寫，camelCase 資料欄（fundingRate）遭殃（2026-07-07，C-17/C-18/C-19）

舊版只為了相容 yfinance `'Date'` vs `'date'` 這一種 index 欄大小寫問題，卻把**所有**欄位
（含 `fundingRate` 這種資料欄）都轉小寫，導致：快取一旦成功寫入，消費端讀 camelCase
全部讀不到；第二輪增量 `concat([既有小寫欄, 新抓 camel 欄])` 產生大小寫分裂雙欄，
`to_sql` 寫回時 SQLite 因欄名大小寫不敏感直接 `duplicate column name` 崩潰。修法：只在
`index_col` 找不到時才嘗試不分大小寫改名，其餘欄位一律保留寫入時原始大小寫。同案：
`onchain.py:fetch_aux_history` 的 httpx/Bybit/OKX 補救路徑成功後主動回寫 SQLite 快取
（ccxt 路徑在所有現行環境皆卡死，故快取表過去從未真正落地磁碟，只留在
`@st.cache_data` 記憶體）；`collector/btc_price_collector.py` 的 Binance K 線抓取補齊
`< end_ms` 年界過濾（C-18，與 Kraken 分支對齊，避免年檔尾根跨入下一年）；Kraken
2013-2016 回補文件誠實化＋預設 `--from-year` 改 2017（C-19，Kraken OHLC 端點實測
只回最近約 720 根、無法深翻歷史，程式碼保留但不再預設觸發）。詳見
`Github\_governance\AUDIT-data-internals-batch4.md` C-17/C-18/C-19；回歸測試見
`tests/test_market_data.py` 新增兩項。
