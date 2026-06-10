# CLAUDE.md — Cow（BTC 投資戰情室）

**路徑：** `D:\Users\63191\Documents\GitHub\Cow`
**目前版本：** v3.10
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
all-in = 電費 × 1.6。

**回測關鍵發現（2015/2018/2022 三輪熊底，`tests/bottom_floors_backtest.py`）**：
- **熊底/純電費 = 1.98→1.10→1.06x**（收斂、**從未跌破** → 電費=硬地板）
- **熊底/all-in = 1.24/0.69/0.67x**（2018/2022 牛末跌破 all-in 至 ~0.67×）
- 2022 熊底 $15,476 落在 Balanced($11.4k)~Realized($20.5k) 之間
- 現況（2026-06）電費 ~$53.5k／all-in ~$85.6k → 「1.06×電費」與「0.67×all-in」收斂於 ~$57k

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

- **逃頂五維（30/25/20/15/10）**：合約過熱 / 技術衰竭 / 鏈上派發 / 情緒過熱 / 總經逆風。
  `compute_escape_top_score` + `escape_top_meta`。
- **抄底六維（25/20/20/15/10/10）**：長週期深跌 / 合約超冷 / 技術回穩 / 情緒恐慌 / 鏈上吸籌 / 總經順風。
  `compute_relative_low_score` + `relative_low_meta`。
- **權重由敏感度測試決定**（`tests/relative_high_backtest.py` / `relative_low_backtest.py`，
  鏡像方法：swing 高/低點 + 其後 60 日反向 18% 為正樣本、時序 train/test、Mann-Whitney U AUC、
  grid search）。**樣本少 → grid 必過擬合 → 採專家配重 + 單維 AUC 排序微調**。
- **兩側天生非對稱**：底部最強維度是「長週期深跌」(AUC 0.662)，頂部是「合約過熱」——
  底部靠估值便宜、頂部靠槓桿過熱。這不是設計缺陷，是市場結構。
- **趨勢方向第三軸（2026-06-09）**：逃頂/抄底是「貴不貴」相對估值量表，`trend_direction`
  補正交的「風往哪吹」：均線結構±40 / MACD±30 / 斜率±15 / ADX±15 → 有號淨分 [-100,+100]。
  ADX<20 時方向三維打 0.6 折（盤整防假突破）。可同時「強多頭＋逃頂高」或「空頭＋抄底高」
  （勿純憑估值接刀），三軸合看。LINE Flex 顯示在波段雷達 box 頂部橫幅。
- **未擬合維度**標 `UNFITTED_DIMS` / `UNFITTED_DIMS_LOW`：OI 自建快照、ETF(2024+)、
  負費率、總經（需行事曆）。

**BTC_WATCH.py 共用**：純幣安環境只連 fapi/dapi + path import 算分。OI 用
`openInterestHist`（5m×13 滾動清洗 + 1d×30 分位）取代失效的相鄰 60s 差值；防線用
`bottom_floors.final_low`（fallback 54000）。可得天花板：逃頂 65、抄底 75
（已接 alternative.me F&G；BTC.D/ETF/SOPR/總經無資料源 → 灰燈）。

詳見 PLAN：`Obsidian/Github/Cow/20260608plan_相對高點判斷.md`、`20260609plan_相對底部判斷.md`。

---

## 已知陷阱

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

### 3. pandas Series `.get()` 不可靠

`curr.get('RSI_14', 50)` 在 Series 上有時不回傳預設值。
修法：`float(curr['RSI_14']) if 'RSI_14' in curr.index else 50.0`

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

### 7. `dict.get(key, default)` 對 `None` 值不觸發預設

`data = {'source': None}`；`data.get('source', '模擬值')` → 回傳 `None`。
修法：`data.get('source') or '模擬值'`

### 8. `reindex(method='nearest')` 早於資料起點填充定值

```python
# fund_hist 從 2021 年起，chart_df 從 2015 年起
fund_sub = fund_hist.reindex(chart_df.index, method='nearest')
# 2021 年前會填成第一筆值（常數線）→ 手動清除
fund_sub.loc[fund_sub.index < fund_hist.index[0]] = np.nan
```

### 9. 資金費率即時備援鏈

| 來源 | URL | 欄位 |
|------|-----|------|
| Binance fapi | `fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT` | `lastFundingRate` × 100 |
| Bybit | `api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT` | `result.list[0].fundingRate` × 100 |
| OKX | `www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` | `data[0].fundingRate` × 100 |

### 10. 圖表欄位名稱與顯示標籤混淆

欄位名 `EMA_20`（含底線）直接用在圖例易被誤讀為 `SMA 20`。
修法：`_ma_label(col)` helper → `col.replace("_", " ")` → `EMA 20` / `SMA 50`。
當 `exit_ma_key == 'EMA_20'` 時，進場線與防守線同一條，合併標籤：`"EMA 20 (進場 ＆ 防守線)"`。

### 11. AHR999 冪律公式錯誤（舊版膨脹至 $177 萬）

根因：舊版用線性指數模型 `10^(2.68 + 0.00057×days)`，2026 年後估值嚴重膨脹。
修法：改用 Giovanni Santostasi 冪律模型：

```python
# ✅ 正確
estimated_price = 10 ** (-17.01467 + 5.84 * np.log10(days_since_genesis))
# ❌ 舊版（勿用）
estimated_price = 10 ** (2.68 + 0.00057 * days_since_genesis)
```

### 12. Walk-Forward 雙重移位 Bug

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

### 13. Walk-Forward 進場乖離硬編碼 1.5% 上限

根因：`dist_pct <= 1.5` 硬編碼造成極少進場（ROI -22% vs swing +1654%），與 swing.py 行為不一致。
修法：改為可選參數 `entry_dist_max_pct`（預設 `None` = 無上限）；UI 提供滑桿，設為 0 = 不限。

### 14. 派網 Bot API 不支援幣本位網格與馬丁格爾

派網官方 Bot Open API（`/api/v1/bot/orders`）的 `buOrderTypes` 只有三種：
`futures_grid`（U本位期貨網格）、`spot_grid`（現貨網格）、`smart_copy`（智能跟單）。

**幣本位合約網格做多** 與 **馬丁格爾機器人** 均不在清單內，且 App 手動建立的機器人也不會出現在此 API 回傳結果。帳戶餘額 API 同樣不含機器人內的資產。

→ 派網 API Key 對本專案目前無用，勿再嘗試讀取機器人狀態。

### 15. GitHub Actions 用量：Cow 為公開 repo，不計入免費額度

GitHub Actions 免費方案：私有 repo 2,000 分鐘/月，**公開 repo 無限制**。

Cow 是公開 repo，`price_alert.yml`（每小時）與 `daily_line_notify.yml`（每天 3 次：台灣 08:23 / 13:39 / 18:27）均不消耗配額。
目前唯一消耗私有配額的是 `Notion_auto`（約 150 分鐘/月），距上限 2,000 分鐘仍有大量餘裕。

### 16. Gemini 2.5 系列為 reasoning 模型，預設 thinking 吃光 output token

根因：`gemini-2.5-flash` 預設開啟「思考」，會把 `maxOutputTokens` 消耗在 thinking 上，
導致 `candidates` 無實際文字輸出（症狀：呼叫耗時很久 + 回傳空）。翻譯/摘要不需思考。
修法：`generationConfig.thinkingConfig.thinkingBudget = 0` 關閉思考（見 `core/gemini_client.py`）。

### 17. Gemini ListModels 會列出已下架模型

`v1beta/models` 清單含 `gemini-2.0-flash`，但實際 `generateContent` 回 404
「This model is no longer available」。**不要以 ListModels 有列出就當可用**，需實打驗證。
目前可用：`gemini-2.5-flash`。

### 18. 新聞來源：Reddit 免認證被 IP 封鎖、X 免費 API 已關閉

- Reddit `hot.json` 對公司網路 IP 與雲端共享 IP 均回 403（IP 層級，換 User-Agent 無效），
  需 OAuth 才穩定 → 改用 **CoinGecko `/search/trending`** 當社群熱度指標（免金鑰）。
- X/Twitter 免費 API 已關閉，無穩定免費抓法 → 不納入。
- 新聞媒體源：CryptoCompare News + Cointelegraph/CoinDesk/Decrypt RSS，聚合去重（`service/news.py`）。

### 19. 新聞中文化的省 token 三層機制（`service/news_i18n.py`）

Gemini 翻譯成本與「Streamlit Cloud 休眠/喚醒」脫鉤的關鍵：
1. 批次：一次 prompt 處理整批（最多 8 則）回 JSON，避免逐則往返。
2. 持久化快取 `db/news_i18n.json`：以 url 為 key，翻過的**永不重翻**（記憶體 `@st.cache_data`
   在 cold start 會清空，故需落地檔案才能跨休眠續用）。
3. 總開關 `NEWS_I18N_ENABLED=false` 可完全停用翻譯（0 API 呼叫）。
   - 休眠時不執行 script → 0 token；真正成本 = 「新出現且沒翻過的新聞則數」。

### 20. requirements 鎖版本：勿替 numpy/pandas 加上限（pandas-ta 0.4.x 需 pandas>=2.3.2）

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
