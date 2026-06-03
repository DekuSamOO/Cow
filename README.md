# Cow — 比特幣投資戰情室 v3.5

> 比特幣多週期量化分析工具，整合技術指標、鏈上數據、期權與波段策略。

**Live App:** https://mfyyo9qf5mymsrouxkfdgj.streamlit.app

---

## 功能總覽

| Tab | 名稱 | 核心功能 |
|-----|------|----------|
| 1 | 🧭 長週期羅盤 | -100~+100 牛熊複合評分 + 油表 Gauge、AHR999/MVRV/Pi Cycle 等 8 大指標底部探測（含**指標明細分解 expander**）、三層分析框架（散戶/機構/宏觀）、市場相位判斷、M2/CPI/日圓/量子威脅、**四季理論目標價預測** |
| 2 | 🌊 波段狙擊 | Antigravity v4.1 進出場信號（EMA20+SMA200+RSI+MACD+ADX 五合一過濾）、**2x3 條件監控儀表板**、動態策略建議、自訂防守線（SMA50/EMA20/SMA200）、OI 未平倉量、Kelly 倉位計算機 |
| 3 | 💰 雙幣理財 | Black-Scholes APY 試算、行權價梯形視覺化、Delta 風險估算、動態無風險利率 |
| 4 | ⏳ 時光機回測 | 自訂區間波段 PnL（可調參數滑桿 + 🔬 最佳參數搜尋，並行加速）、雙幣滾倉回測、牛市雷達準確度驗證（含 MA50 視覺化）、**📈 多週期回測（日線宏觀過濾 + 15m 精確進場，防先視偏誤）**、**🚀 Walk-Forward 無先視回測（逐日推進，可選簡化或六層進階出場機制）** |
| 📰 | 加密新聞輿情 | Dashboard 速覽下方：CryptoCompare/Cointelegraph/CoinDesk/Decrypt 多來源聚合去重、**Gemini 中文化標題＋小結**、AI 情緒燈號、分類 filter、CoinGecko 24h 熱搜 |
| 🤖 | 決策速報推播 | 透過 GitHub Actions 每日三時段 (台灣 **08:23 / 13:39 / 18:27**) 自動抓取大盤與指標數據，發送高質感 Flex Message 決策面板至 LINE（含**新聞輿情區塊**）|

---

## 架構

```text
app.py              入口點（組合各層，不含業務邏輯；今日大盤速覽 6 大 Metric 以 @st.fragment(run_every=60) 每 60 秒自動更新）
config.py           集中設定（均線週期、交易成本、倉位風控參數、WALK_FORWARD_EXIT_MODES）
data_manager.py     根層級數據管理器（TVL/穩定幣/資金費率歷史 SQLite 快取、指數退避重試、增量模式）

collector/
  btc_price_collector.py  本地端 15m K 線收集器（Binance + Kraken 雙源，年度 SQLite 分割，支援 --push）

db/                 年度分割 SQLite 資料庫（本地收集後 push 至雲端，Streamlit 直接讀 repo 內 db）
  btcusdt_15m_2013.db
  ...
  btcusdt_15m_2026.db

core/
  indicators.py       技術指標 + AHR999 計算（純函數，無 Streamlit 依賴）
                      AHR999 使用 Giovanni Santostasi 冪律：10^(-17.01467 + 5.84×log10(days))
  bear_bottom.py      熊市底部 8 大指標評分引擎 + -100~+100 牛熊複合評分（含 breakdown 分解）
  season_forecast.py  四季理論目標價預測引擎（減半週期判斷、歷史倍數遞減模型、冪律走廊）
  gemini_client.py    Gemini REST API 輕量封裝（關閉 thinking budget 省 token、x-goog-api-key header，供新聞中文化）

service/
  local_db_reader.py  讀取本地 SQLite（15m 原始 / 重採樣日線），TTL 快取，全面 UTC 時區
  market_data.py      BTC / DXY 歷史數據（五層備援：本地DB→Yahoo→Binance→Kraken→CryptoCompare）
                      + T 日數據縫合，全面 UTC 時區（修正 UTC+8 偏移 8 小時 bug）
  onchain.py          鏈上輔助數據（非同步 httpx 並行，TVL/穩定幣/資金費率歷史）
  realtime.py         即時報價（Binance→Kraken→本地DB 三層備援）
                      含資金費率/OI（Binance→Bybit→OKX 三層），Header 偽裝與 SSL 繞過
                      各欄位追蹤 price_source / funding_rate_source / tvl_source
  macro_data.py       宏觀數據（FRED M2/CPI、Yahoo 日圓、量子威脅）+ 全面靜態備援 _FALLBACK 字典 (v1.1)
  mock.py             代理指標與模擬數據（API 失敗降級備援）
  overview.py         今日大盤速覽指標降級解析（主流程與 fragment 共用 helper，含 funding/tvl is_real 旗標）
  news.py             加密新聞多來源聚合去重（CryptoCompare/Cointelegraph/CoinDesk/Decrypt + CoinGecko 24h 熱搜）
  news_i18n.py        Gemini 批次中文化（標題＋小結＋情緒）+ db/news_i18n.json 持久化快取（翻過不重翻）

strategy/
  swing.py              Antigravity v4.1 波段策略引擎（防先視偏誤、日頻 Sharpe、多週期回測引擎）
  walkforward_backtest.py  Walk-Forward 無先視回測器（逐日推進、六層出場機制、績效統計）
  dual_invest.py        雙幣期權策略引擎（Black-Scholes，動態無風險利率）
  notifier.py           LINE Bot 主動推播通知模組

scripts/
  daily_line_notify.py     GitHub Actions 雲端自動推播腳本（Kraken 備援，台灣 08:23 / 13:39 / 18:27 三時段，含新聞輿情）
  test_flex_message.py     本地端測試 LINE Flex Message 排版的除錯腳本
  test_compare_backtest.py 驗證腳本：對相同參數同時執行 swing.py 與 Walk-Forward，確認結果量級一致

handler/
  layout.py          頁面設置、側欄（只保留日期區間，策略參數移至各 Tab）
  tab_macro_compass.py Tab 1：長週期羅盤（雙 Gauge + 評分公式 expander + 三層框架 + 底部 8 指標 + 四季預測）
  tab_swing.py       Tab 2：波段狙擊（3 行式 K 線子圖、2x3 條件儀表板、動態建議、倉位計算）
  tab_dual_invest.py Tab 3：雙幣理財（行權價梯形視覺化）
  tab_backtest.py    Tab 4：時光機回測（5 個子 Tab：波段 PnL、雙幣滾倉、牛市雷達、多週期回測、Walk-Forward 無先視）

tests/
  test_bear_bottom.py   熊市底部指標單元測試
  test_dual_invest.py   雙幣期權策略單元測試
  test_market_data.py   數據來源與備援鏈單元測試
  test_news.py          新聞聚合/去重/情緒彙總/中文化降級單元測試（monkeypatch 不打真 API）
```

---

## 核心指標說明

### Tab 1：🧭 長週期羅盤

將總體經濟、鏈上數據與技術分析融合，提供由宏觀到微觀的完整市場週期定位。

#### 1. 綜合牛熊狀態 (Market Cycle Score)
由 8 組對稱指標組成，計算出 **-100 ~ +100 的牛熊複合評分**（`bull_total - bear_total`）。
* **-100** = 極度深熊（All-In 信號）
* **0** = 中性區間
* **+100** = 狂熱牛頂（逃頂信號）

搭配 Plotly `go.Indicator` 油表雙量程顯示，並輔以 0-5 級的「市場相位量表」。

油錶圖下方新增 **📐 多空評分計算公式** 展開面板，可即時查看 8 大指標各自的當前值、熊底貢獻分數、牛頂貢獻分數與淨貢獻（牛-熊），方便驗證分數合理性。分數若長時間維持穩定屬正常現象，代表鏈上週期確實處於同一區間。

#### 2. 三層分析框架 (Micro to Macro)
| 層次 | 指標 | 說明 |
|------|------|------|
| **散戶視角 (Level 1)** | 趨勢結構 | SMA50 vs SMA200 黃金/死亡交叉 + 年線斜率 |
| | 道氏理論 | 20日高點 vs 前20日高點（HH/LH 判斷） |
| | 情緒指數 | Alternative.me Fear & Greed，或 RSI/動能代理 |
| **機構視角 (Level 2)** | AHR999 | (Price/SMA200)×(Price/指數增長估值)，< 0.45 = 歷史抄底 |
| | MVRV Z-Score | (Price - SMA200) / 200日標準差，< 0 = 低估 |
| | BTC 生態 TVL | DeFiLlama 鏈上鎖倉量，判斷資金流入/流出趨勢 |
| | 資金費率 | 幣安永續合約當期費率，> 0.03% = 多頭過熱 |
| **宏觀視角 (Level 3)** | BTC vs DXY | 90日滾動相關係數，< -0.5 = 正常負相關 |
| | 全球穩定幣市值 | 流動性代理，> $100B = 流動性充沛 |
| | 美國 M2 | FRED WM2NS 週頻，流動性環境評估 |
| | 日圓匯率 | USD/JPY，套息交易 (Carry Trade) 風險指標 |

#### 3. 熊市底部探測 (8 大指標)
專門用於尋找歷史級別的長期買點，總分 100 分。評分 75+ 代表歷史極值底部，45 以下代表脫離底部區間。
包含：**AHR999** (滿分20)、**MVRV Z-Score** (18)、**Pi Cycle Gap** (15)、**200週均線比值** (15)、**Puell Multiple** (12)、**月線 RSI** (10)、**冪律支撐倍數** (5)、**Mayer Multiple** (5)。

#### 4. 四季理論目標價預測
基於比特幣減半週期（約4年）劃分四季，整合歷史漲跌倍數遞減規律與冪律模型，預測未來 12 個月目標價。
* **春 (月0-11)**：多頭啟動 / **夏 (月12-23)**：FOMO蔓延 / **秋 (月24-35)**：空頭確立 / **冬 (月36-47)**：恐慌拋售。
* 演算法結合「歷史中位數倍數遞減 (÷3.5) 模型」與「冪律走廊 (Power Law)」，給出保守、中位數、樂觀三種目標價與信心區間。

---

### Tab 2：🌊 波段狙擊 (Antigravity v4.1)

專為中期波段交易設計，結合趨勢過濾與動能確認，並具備嚴格的出場防守機制。

#### 1. 進場條件 (五合一過濾，全部滿足才亮燈)
改用 **2x3 條件監控儀表板** 顯示，全數通過即觸發買進建議：
1. **趨勢向上**：Price > SMA200 (年線多頭)
2. **動能偏多**：RSI_14 > 50
3. **MACD 金叉**：MACD > Signal
4. **趨勢成型**：ADX > 20 (過濾無方向盤整)
5. **資金健康**：資金費率 < 0.05% (未過熱)
6. **站上短均**：Price ≥ EMA20 (解除原乖離限制，改抓突破)

#### 2. 動態出場防守線
使用者可從 UI 下拉選單自訂波段防守線（**SMA 50**, **EMA 20**, **SMA 200**）。當價格跌破選定均線時，即觸發紅色出場信號。選擇 EMA 20 時，進場參考線與防守線合併為同一條線，圖例自動更新為「EMA 20 (進場 ＆ 防守線)」，不重複繪製。

#### 3. 輔助決策模組
* **綜合策略建議**：依據當前乖離率、RSI、趨勢狀態，動態給出「絕佳進場買點」、「乖離過大不宜追高」、「跌破短期均線觀望」等文字與顏色提示。
* **未平倉量 (OI) 監控**：即時抓取 Binance 永續合約 OI 與 60 秒變化率，輔助判斷趨勢是建倉推動還是平倉衰竭。
* **Kelly 倉位計算機**：輸入總本金與單筆風險 (1-5%)，依據進場價與防守線距離，自動計算安全的開倉 BTC 數量與建議槓桿。

---

### Tab 3：💰 雙幣理財顧問

協助評估 CEX 雙幣理財產品（BTC/USDT 結構型期權），透過 Black-Scholes 定價與視覺化梯形圖，判斷各檔行權價的 APY 合理性。

#### 1. 行權價梯形視覺化
以近 60 日 K 線為背景，疊加 SELL_HIGH（看漲行權，紅色水平線）與 BUY_LOW（看跌行權，綠色水平線）梯度，輔以 **ATR 隱含波動帶**（`±ATR × √t_days`）標示持有期間的預期波動範圍。一眼判斷行權價落在波動帶內外，評估行使風險。

#### 2. APY 試算與機會成本雷達
* **Black-Scholes 定價**：依據歷史波動率（HV）、無風險利率（DeFiLlama Aave V3 USDT 動態抓取）、行權價距離，計算各檔理論期權價值與對應 APY。
* **APY 對比長條圖**：各檔行權價 APY 與 DeFi 活存利率並排，直觀判斷哪些產品提供了真實的超額回報。
* **Delta 風險估算**：顯示各檔行權價的 Delta 值，評估行使概率（Delta 越接近 0.5 代表行使風險越高）。

#### 3. 使用範例
1. 選擇產品類型（SELL_HIGH / BUY_LOW）與持有天數（3/7/14/30 天）
2. 梯形圖顯示各檔行權價位置，對比 ATR 波動帶判斷行使風險
3. APY 雷達圖確認是否優於 DeFi 活存利率（機會成本）
4. 選擇 Delta < 0.3 的行權價以降低行使概率，同時確保 APY > DeFi 利率 × 1.5

---

### Tab 4：⏳ 時光機回測

包含 5 個子分頁，提供從簡單波段到嚴格防先視偏誤的完整回測工具箱。

#### 子分頁 1：📉 波段策略 PnL
在任意歷史區間內驗證 Antigravity 策略績效：
* **可調參數**：EMA20 乖離閾值、RSI 閾值（40-65）、ADX 閾值（10-35）、波段防守線（SMA50/EMA20/SMA200）
* **🔬 最佳參數搜尋**：Grid Search 並行掃描所有參數組合（ThreadPoolExecutor × 4），可針對「最高勝率」或「最高 ROI」最佳化
* **輸出**：資金曲線圖、交易明細（含進出場價、持倉天數、報酬率）、Sharpe Ratio（日線市值曲線計算）、CSV 下載

#### 子分頁 2：💰 雙幣滾倉回測
模擬歷史上持續滾動操作雙幣理財的長期績效，對比買入持有 (Buy & Hold)。

#### 子分頁 3：🐂 牛市雷達準確度
驗證長週期羅盤評分的歷史預測能力：回測每次評分觸發特定閾值後的後續報酬，同時疊加 MA50 視覺化。

#### 子分頁 4：📈 多週期回測 (Multi-TF)
結合日線宏觀過濾 + 15 分鐘精確進場，消除純日線回測的時間解析度不足：
* 日線層（SMA200 / 金叉）做多頭環境過濾，`shift(1)` 確認後才允許進場
* 15m 層執行 EMA/RSI 進場判斷，同樣 `shift(1)` 執行（次根 K 線開盤成交）
* 支援固定停損百分比，買賣點疊加日線圖展示

#### 子分頁 5：🚀 Walk-Forward 無先視回測
最嚴格的回測模式，採逐日推進迴圈，每日只能看到當日及以前的資料：

| 模式 | 出場機制 | 適用場景 |
|------|----------|----------|
| **簡化模式** | 防守線 EMA 跌破（昨日收盤 < 均線 → 今日開盤出場） | 長期持倉、結果與 swing.py 可比較驗證 |
| **進階模式** | 六層優先級出場（Climax→ATR停損→ATR目標→Chandelier→Time Stop→EMA） | 短中期波段、精細風控 |

六層出場優先級：
1. **Climax Exit**：正乖離 > 30% 或爆量長上影線（散戶 FOMO 頂部訊號）
2. **ATR 停損**：進場後跌破 `進場價 - N×ATR`
3. **ATR 目標**：達到 `進場價 + M×ATR` 獲利了結
4. **Chandelier Exit**：追蹤止利（N 日最高點 - K×ATR）
5. **Time Stop**：持倉 ≥ 15 日且淨報酬 < 5%，強制出場
6. **EMA 停損**：跌破防守均線（最後防線）

---

## 使用範例

### 場景 1：判斷當前是否適合加倉
1. 打開 **Tab 1（長週期羅盤）**，查看牛熊評分（-100~+100）
2. 評分 < -30 且熊市底部探測 > 60 分 → 歷史級別買點，可重倉
3. 展開「📐 多空評分計算公式」確認各指標貢獻，避免單一指標失真
4. 切換到 **Tab 2（波段狙擊）**，確認 2x3 條件儀表板全部綠燈
5. 用 Kelly 計算機輸入本金與停損距離，計算建議倉位

### 場景 2：驗證策略參數後再實戰
1. 打開 **Tab 4（時光機回測）→ 子分頁 5（Walk-Forward）**
2. 選擇「簡化模式」，設定回測區間（建議 2+ 年）
3. 調整 EMA20 乖離上限、RSI 閾值，觀察 ROI 與 Sharpe 變化
4. 點選「🔬 尋找最佳參數」做 Grid Search 驗證
5. 對比子分頁 1（波段 PnL）的結果，確認兩者交易次數一致（排查先視偏誤）

### 場景 3：評估雙幣理財產品
1. 打開 **Tab 3（雙幣理財）**，選擇 SELL_HIGH（看漲期權）或 BUY_LOW（看跌期權）
2. 設定產品期限（如 7 天）
3. 梯形圖查看各行權價落點：選擇在 ATR 波動帶外的行權價（被行使概率較低）
4. 對比 APY 長條圖，確認 APY 顯著高於 DeFi 活存利率才值得操作

### 場景 4：設定每日自動推播
```bash
# 1. Fork 本 Repo 並在 GitHub 設定 Secrets
#    LINE_CHANNEL_ACCESS_TOKEN=<你的 Token>
#    LINE_USER_ID=<你的 User ID>
#    GOOGLE_API_KEY=<Gemini 金鑰，供新聞中文化；不設則推播新聞顯示英文>

# 2. 本地測試 Flex Message 排版
python scripts/test_flex_message.py

# 3. 推送後 GitHub Actions 將在每日 08:23 / 13:39 / 18:27（台灣時間）三時段自動發送
```

---

## 數據來源

| 類別 | 來源 | 說明 |
|------|------|------|
| BTC 15m K 線 | **本地 SQLite DB** (0th) | 由 collector 預先收集並 push 至 repo，Streamlit 直接讀取 |
| BTC 歷史 OHLCV | Yahoo Finance → Binance → Kraken → **CryptoCompare** | 四層備援（無本地 DB 時啟用），覆蓋 2010 年起完整歷史 |
| 即時價格 | Binance → **Kraken** → **本地 15m DB** | 三層備援；企業防火牆封鎖 Binance 時自動切換，UI 標示目前來源 |
| 即時 OI/資金費率 | Binance → **Bybit** → **OKX** | 三層備援；Binance fapi 遭封鎖時自動切換，UI 標示目前來源 |
| 鏈上 TVL | DeFiLlama API | Bitcoin DeFi 生態鎖倉量 |
| 穩定幣市值 | DeFiLlama API | 全球穩定幣流通量 |
| 恐懼貪婪指數 | Alternative.me | 市場情緒代理 |
| M2 / CPI / 日圓 | FRED 公開 CSV API | 宏觀流動性指標 |
| DeFi 無風險利率 | DeFiLlama (Aave V3 USDT) | 雙幣策略動態折現 |

---

## 本地執行

```bash
pip install -r requirements.txt
# 設定 API Key（可選，不設定仍可運作）
cp .env.example .env
# 填入 BINANCE_API_KEY, LINE_CHANNEL_ACCESS_TOKEN 等
streamlit run app.py
```

---

## 🤖 LINE 決策速報自動推播設定 (GitHub Actions)

本功能將每日市場快照升級為「決策輔助面板」，透過 GitHub Actions 定時觸發，無需本機常駐即可自動發送高質感 LINE Flex Message。

**三時段排程：** 已在 `.github/workflows/daily_line_notify.yml` 中設定每日自動執行（沿用畸零分鐘避開免費版 Actions 整點壅塞、調早避免延遲拖到深夜）：
- UTC 00:23（台灣時間 **08:23**）— 早盤決策參考
- UTC 05:39（台灣時間 **13:39**）— 午後盤勢確認
- UTC 10:27（台灣時間 **18:27**）— 傍晚收盤總結

**設定步驟：**
1. GitHub Repo → **Settings → Secrets and variables → Actions**
2. 新增 `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`，以及 `GOOGLE_API_KEY`（新聞中文化；未設則推播新聞顯示英文標題）。
3. 推送後 Actions 將依排程自動執行，亦可手動觸發 `workflow_dispatch` 測試。
4. **Streamlit Cloud** 網頁版若要中文新聞，另需在 App settings → Secrets 加 `GOOGLE_API_KEY = "..."`。

**本地端除錯：**
```bash
# 使用 test_flex_message.py 在本地預覽 Flex Message 排版，不實際發送至 LINE
python scripts/test_flex_message.py
```

---

## 歷史數據收集器

Streamlit Cloud 因 IP 封鎖等原因有時無法取得完整歷史數據。
**解法：在本地端一次性收集，commit push 到 GitHub，雲端直接讀取 repo 內的 SQLite 檔案。**

```bash
# 日常增量更新（自動 push 至 Repo）
python collector/btc_price_collector.py --push

# 只更新特定年份
python collector/btc_price_collector.py --year 2021 --push
```

### 五層備援優先序
1. `0th` 本地 SQLite DB（最穩，毫秒級讀取）
2. `1st` Yahoo Finance（加入 User-Agent 偽裝）
3. `2nd` Binance REST（部分 Cloud IP 受封鎖）
4. `3rd` Kraken（無地理封鎖）
5. `4th` CryptoCompare（最強歷史覆蓋率）

---

## Streamlit 防休眠設定

Streamlit Community Cloud 在 **7 天無流量**後自動休眠。本專案使用 GitHub Actions 每日兩次自動 Ping 保持喚醒。
* 於 Repo Secrets 新增 `STREAMLIT_APP_URL` 即可啟動 `.github/workflows/keepalive.yml`。

---

## 版本紀錄

### v3.5 (2026-06-03)
- **feat(news)**: Dashboard 速覽下方新增「📰 加密貨幣熱門新聞」區塊：`service/news.py` 多來源聚合去重（CryptoCompare News + Cointelegraph/CoinDesk/Decrypt RSS），CoinGecko `/search/trending` 24h 熱搜取代被 IP 封鎖的 Reddit。
- **feat(news_i18n)**: `service/news_i18n.py` + `core/gemini_client.py` 以 Gemini 2.5-flash（關閉 thinking budget）批次翻譯標題、產生中文小結與情緒判定；`db/news_i18n.json` 持久化快取（翻過不重翻）+ `@st.cache_data(ttl=14400)` 4h + `NEWS_I18N_ENABLED` 總開關三層省 token（估約 US$0.3/月）。
- **feat(app)**: 新聞區塊含情緒燈號（多空中性彙總）、分類 filter（BTC/ETH/DeFi/法規）、社群熱搜；情緒彙總抽 `summarize_sentiment` 供 UI 與推播共用。
- **feat(notify)**: 每日 LINE 推播 Flex 新增「📰 加密新聞輿情」區塊（情緒燈號 + 8 則中文標題）；排程調早為台灣 **08:23 / 13:39 / 18:27**（沿用畸零分鐘避開整點壅塞）。
- **refactor(overview)**: 速覽降級邏輯抽 `service/overview.py::resolve_overview_metrics`，主流程與 fragment 共用；新增 funding/tvl `is_real` 旗標，資料暫缺時顯示「—」不再給假數字。AHR999 標註冪律 (Santostasi) 來源。
- **test**: 新增 `tests/test_news.py`（9 passed，全 monkeypatch 不打真 API）。

### v3.4 (2026-05-04)
- **fix(notify)**: `scripts/daily_line_notify.py` 修復礦工電費指標靜默消失問題：廢棄的 `blockchain.info/q/hashrate`（404）改用 `api.blockchain.info/stats` 的 `hash_rate` 欄位，並新增 `mempool.space/api/v1/mining/hashrate/1d` 為備援來源（H/s → TH/s 換算）。
- **fix(security)**: 同檔全面將硬編碼 `verify=False` 改為 `verify=SSL_VERIFY`，涵蓋 blockchain.info、mempool.space、Coinbase 即時價格、LINE 推播共 4 處。
- **refactor(notify)**: 抽取 `_miner_cost_from_ths()` 為模組層級函式，消除兩處重複的算力→電費換算內聯邏輯；刪除 `# [修改區塊]` 等說明性冗長注解。
- **feat(actions)**: `.github/workflows/daily_line_notify.yml` 新增第三個排程時段 UTC 13:27（台灣時間 21:27），改為三時段推播。

### v3.3 (2026-03-20)
- **fix(security)**: `scripts/daily_line_notify.py` 移除 `CURL_CA_BUNDLE` / `REQUESTS_CA_BUNDLE` 強制清空（在 GitHub Actions 雲端環境中會完全停用 SSL 驗證），改為從 `config.SSL_VERIFY` 統一讀取，與其他所有模組行為一致。
- **fix(security)**: `strategy/notifier.py` LINE 推播由硬編碼 `verify=False` 改為受 `config.SSL_VERIFY` 控制；僅本地端開發才靜默 urllib3 警告；同步修正 timeout 日誌顯示 5s 實際 8s 的錯誤。
- **fix(quality)**: `strategy/swing.py` 移除純計算模組中無意義的全域 `urllib3.disable_warnings`（此模組無任何 HTTP 請求）。
- **fix(quality)**: `data_manager.py` 新增 `_VALID_TABLES` 白名單驗證防止 SQL table name injection；修正 Python 3.12+ 棄用的 `datetime.utcfromtimestamp()` 改用 `datetime.fromtimestamp(tz=utc)`。
- **fix(quality)**: `app.py` 移除 `__import__('pandas')` 反模式，改用頂層 `import pandas as pd`。
- **perf**: `app.py` 主流程抓取 `fetch_realtime_data()` 後存入 `st.session_state` 快取（TTL 30s），fragment 首次觸發時重用，避免頁面初次載入時的重複 API 請求。
- **perf**: `strategy/dual_invest.py` 合併 `_fetch_aave_usdt_rate()` 與 `_fetch_makerdao_dsr()` 兩次獨立 DeFiLlama pools 請求為單次 `_fetch_defi_risk_free_rate()`，減少重複下載同一份大型 JSON。
- **fix(reliability)**: `service/local_db_reader.py` SQLite 連線改用 `with` context manager，確保例外發生時連線仍會正確關閉。
- **fix(reliability)**: `service/onchain.py` 精確化 `RuntimeError` 捕捉範圍，僅攔截 event loop 衝突錯誤，其他 RuntimeError 正常向上傳遞。
- **fix(infra)**: `requirements.txt` 移除重複的 `pandas_ta` / `pandas-ta` 條目；為所有套件加入最低版本限制，確保部署一致性。
- **fix(infra)**: `tests/test_market_data.py` 移除引用不存在函式（`_normalize_yf_columns`、`_download_yf`）的失效測試；新增白名單拒絕非法表格的安全驗證測試。
- **fix(infra)**: `service/realtime.py` `defi_yield` 加入 `defi_yield_is_mock=True` 旗標，UI 層可正確標示為模擬值而非真實 API 數據。
- **refactor**: `config.py` 移除未被任何模組引用的死代碼常數 `ENTRY_DIST_MAX_PCT`；`.env.example` 補充 `IS_STREAMLIT_CLOUD` 設定說明。

### v3.2 (2026-03-10)
- **refactor(walkforward)**: `/simplify` 審查修復：新增 `entry_dist_max_pct < entry_dist_min_pct` 參數驗證（原本靜默產生零交易）；`exit_mode == "simple"` 字串比對由迴圈內移至迴圈前布林旗標 `use_simple_mode`；移除 `len(trades) >= 0` 恆真殘留條件；`_exit_mode_keys` 單次賦值取代 `st.radio` 內兩次 `list(keys())`。

### v3.1 (2026-03-10)
- **fix(walkforward)**: 簡化模式出場邏輯全面對齊 swing.py — 改以向量化 `exit_signal_shifted`（昨日收盤 < 防守線）觸發，今日開盤執行（移除 `pending_exit_simple` 狀態機），消除訊號差一天的問題；修復後兩者交易次數完全一致（37次），ROI 差距從 >1600% 縮至 ~42%（交易次數相同，剩餘差距為進場開盤價的複利效應）。
- **fix(walkforward)**: 移除簡化模式的 `min_hold_days` 持倉下限限制，與 swing.py 行為對齊（次日即可出場）。
- **fix(walkforward)**: 進場執行價由「訊號當日收盤」改為「次日開盤」（`open_vals[i]`），與 swing.py 防先視偏誤設計一致。

### v3.0 (2026-03-10)
- **fix(walkforward)**: 修正 Walk-Forward 進場條件 `dist_pct <= 1.5%` 硬編碼上限造成極少進場（ROI -22% vs swing +1654%）的根本原因；改為可選參數 `entry_dist_max_pct`（預設 `None` = 無上限，與 swing.py 行為一致），UI 新增「EMA20 最大乖離 (%)」滑桿，設為 0 = 不限。
- **fix(walkforward)**: 修正 `bull_trend` 使用 `close_shifted`（前一日收盤）後再統一 shift(1) 造成雙重移位（訊號實際看到 2 天前的資料）；改為所有條件統一使用當日值，最後一次性 shift(1)，與 swing.py 邏輯對齊。
- **fix(walkforward)**: 簡化模式（`exit_mode="simple"`）掃描頻率固定為 1（每日），不再受 `scan_freq` 預設值 5 影響；進階模式才允許 UI 滑桿調整掃描頻率，避免漏掉進場訊號。
- **feat(tab_backtest)**: Walk-Forward 子分頁新增「EMA20 最大乖離 (%) ── 0 = 不限」滑桿（0.0–20.0%），允許用戶自訂甜蜜點上限；簡化模式下掃描頻率固定顯示提示，進階模式才顯示頻率滑桿。
- **test**: 新增 `scripts/test_compare_backtest.py` 驗證腳本，對相同參數同時執行 swing.py 與 Walk-Forward，確認修復後兩者結果量級一致（+1014% vs +769%，差距從 >1600% 縮至 ~245%，剩餘差距為進場執行價 close/open 與 min_hold_days 設計差異）。

### v2.9 (2026-03-10)
- **feat(backtest)**: 新增 Walk-Forward 無先視回測器（`strategy/walkforward_backtest.py`），改編自 tw_stock_climber v4.8 設計：逐日推進迴圈，每日只能看到該日及以前的資料，消除先視誤差。
- **feat(backtest)**: 實作六層出場機制優先級：①Climax Exit（正乖離>30% 或爆量+長上影線）②ATR 停損③ATR 目標④Chandelier Exit（追蹤止利）⑤Time Stop（≥15日 且 報酬<5%）⑥EMA 停損，提供更精細的風險控制。
- **feat(tab_backtest)**: 新增第 5 個子分頁「🚀 Walk-Forward 無先視回測」，支援進場參數調整（EMA 乖離、RSI、ADX）、出場參數自訂（ATR 倍數、防守線選擇）、掃描頻率設定，展示買賣點分析圖表、交易明細、CSV 下載。
- **feat(tab_backtest)**: Walk-Forward 新增 `exit_mode` 參數：**簡化模式**（只用防守線 EMA 跌破）與**進階模式**（全六層機制），用戶可依需求選擇，簡化模式適合長期持倉且獲利較佳。
- **refactor(backtest)**: 移除 `WalkForwardBacktester.max_drawdown()` 重複定義，改為複用 `swing.py::calculate_max_drawdown()`，減少代碼維護負擔。
- **refactor(config)**: 新增 `WALK_FORWARD_EXIT_MODES` 集中管理出場模式設定，解決 stringly-typed code 問題；添加出場模式驗證防止無效參數。
- **fix(backtest)**: 修正廢棄的 Pandas 語法 `.fillna(method='ffill')` → `.ffill()`（pandas 2.0+ 相容性）。
- **fix(backtest)**: 修正 Walk-Forward 多層出場機制的縮排錯誤，確保 Climax Exit 等條件正確嵌套於 `else` 子句中。

### v2.8 (2026-03-05)
- **fix(realtime)**: 資金費率備援鏈由單源 Binance 擴充為三層（Binance fapi → Bybit → OKX），`funding_rate_source` 不再顯示 `None`；修正 `dict.get(key, default)` 對值為 `None` 的 key 不回傳預設值的問題（改用 `or` 語法）。
- **fix(tab_swing)**: 新增 `_ma_label()` 將欄位名稱（`EMA_20`）轉為可讀格式（`EMA 20`），避免與 `SMA 20` 混淆；選擇 EMA 20 作為防守線時，圖表不再重複繪製，進場線標籤合併為「EMA 20 (進場 ＆ 防守線)」。
- **fix(tab_macro_compass/chart)**: 多維度長週期主圖第 3 行資金費率，`reindex(method='nearest')` 導致 2021 年前所有日期填充為第一筆定值；修正為對早於 `fund_hist.index[0]` 的日期填 `NaN`，圖表不再顯示錯誤的橫向常數線。
- **feat(tab_macro_compass/B+C1)**: B 段（Level 1/2/3）與 C1 八大指標卡片全面加入「來源：」標籤；Level 1 標示本地計算，Level 2 各指標標示計算公式/DeFiLlama/即時交易所來源，Level 3 宏觀移除「⚠️ FRED 連線失敗 / 靜態備援值」警示文字，改以 `st.caption` 乾淨顯示數據來源。
- **style(layout)**: 新增 `.metric-source` CSS class 供各 Tab 統一顯示小字來源標籤。

### v2.7 (2026-03-05)
- **fix(realtime)**: 即時價格加入 Kraken 現貨 Ticker 與本地 15m DB 雙層備援，企業防火牆封鎖 Binance 時自動切換，不再 fallback 至歷史日線收盤（靜態值）。
- **feat(app)**: 今日大盤速覽 6 大 Metric 各加「來源：XXX」小字標示（Binance / Kraken / 本地DB / 歷史收盤 / DeFiLlama / Alternative.me / Antigravity Proxy / 歷史計算）。
- **refactor(realtime)**: 新增 `price_source`、`funding_rate_source`、`tvl_source` 欄位，來源追蹤從 service 層傳遞，app.py 不再內嵌 `None` 判斷邏輯（修正 leaky abstraction）。
- **refactor(local_db_reader)**: `get_latest_local_price()` 移除多餘的 `os.path.exists()` 檢查（`get_available_years()` 已確認檔案存在，屬 TOCTOU 反模式），改用 try/except；`get_latest_local_price` import 移至 `realtime.py` 頂層。

### v2.6 (2026-03-05)
- **fix(app)**: `@st.fragment` 參數改為純量 float（5 個：`prev_close`, `fallback_price`, `rsi14`, `sma50`, `ahr999`），避免序列化大型 DataFrame 導致 fragment 靜默失效、今日大盤速覽 BTC 現價停止自動更新的問題。
- **fix(tab_dual_invest)**: 行權梯形圖「現價」基準線改用即時 `current_price`（原為歷史日線收盤 `curr_row['close']`）；快取鍵新增 `price // 1000` bucket，價格移動 \$1,000 才重建圖表。
- **fix(tab_swing)**: Kelly 計算機「預計進場價格」預設值改用即時 `current_price`（原為歷史 `curr['close']`）。
- **refactor(app)**: 以 `math.isnan()` 取代 `ahr999 == ahr999` NaN 判斷（語意更明確）；pandas Series 欄位存取改為 `curr['col'] if 'col' in curr.index else default`，防止指標計算局部失敗時 `KeyError`；`current_price` 加入 `float()` 確保型別一致。

### v2.5 (2026-03-05)
- **fix(app)**: 移除 `streamlit-autorefresh` 第三方套件（在 Streamlit Cloud 新版本上不穩定），改用 Streamlit 內建 `@st.fragment(run_every=60)` 將今日大盤速覽包成 fragment，每 60 秒獨立重跑並重取即時數據，不觸發全頁重載，效率與穩定性皆提升。
- **chore(deps)**: `requirements.txt` 移除 `streamlit-autorefresh`。

### v2.4 (2026-03-05)
- **fix(backtest)**: `run_swing_strategy_backtest` 加入 `shift(1)` 防先視偏誤（Look-Ahead Bias）：訊號在第 N 根收盤確認，改於第 N+1 根**開盤價**執行，而非錯誤地使用訊號當根收盤。
- **fix(backtest)**: Sharpe Ratio 改由**日線市值曲線**（equity_daily）計算年化值，取代原本在逐筆交易報酬上乘 √252 的錯誤公式。
- **feat(swing)**: 新增 `run_multitf_backtest()` 多週期回測引擎：日線宏觀過濾（SMA200/金叉）shift(1) 確認後，才允許在 15m 上進場；15m 訊號同樣 shift(1) 執行，並支援固定停損。
- **feat(tab_backtest)**: 新增「📈 多週期回測 (Multi-TF)」子分頁，含日線過濾開關、15m EMA 週期、RSI 閾值、停損百分比等參數，買賣點疊加在日線圖上呈現，停損出場另以橘色 ✗ 標記。
- **perf(tab_backtest)**: 參數最佳化 Grid Search 改用 `ThreadPoolExecutor(max_workers=4)` 並行執行，速度提升 ~3-4x。
- **fix(timezone)**: `service/market_data.py` 與 `service/local_db_reader.py` 全面改用 `timezone.utc`，修正在 UTC+8 環境下 `datetime.strptime().timestamp()` 偏移 8 小時的 bug（影響 DB 查詢邊界與 Binance/Kraken API 起始時間）。

### v2.3 (2026-03-04)
- **fix(ui)**: 修正 Tab 1 名稱「長週期**週期**羅盤」→「長週期羅盤」（移除重複的「週期」字）。
- **feat(bear_bottom)**: 新增 `calculate_market_cycle_score_breakdown()`，返回 `(score, bear_total, bull_total, indicator_rows)`；重構 `calculate_market_cycle_score()` 呼叫 breakdown 函數，消除邏輯重複。
- **feat(tab_macro_compass)**: 油錶圖下方新增「📐 多空評分計算公式」展開面板，逐指標顯示當前值、熊底分、牛頂分、淨貢獻與合計，方便驗證分數合理性（分數長時間不變屬正常現象，非 bug）。
- **fix(market_data)**: T 日數據縫合成功後即時將結果寫回 `BTC_HISTORY.csv`，避免下次 `fetch_market_data()` 仍重複抓取相同缺口。
- **feat(app)**: 引入 `streamlit-autorefresh`，每 60 秒自動重整頁面（後於 v2.5 改用內建 fragment 替代）。

### v2.2 (2026-02-26)
- **feat(swing)**: Tab 2 波段狙擊 UI 全面升級，新增 2x3 條件監控儀表板、動態「策略建議」區塊與外框卡片設計，並加入動態防守線（SMA_50, EMA_20, SMA_200）自訂功能。
- **perf(strategy)**: 波段策略回測引擎 `strategy/swing.py` 放寬進場乖離限制（改抓突破與趨勢確認），並重構底層邏輯支援動態出場均線。
- **feat(realtime)**: 移除 `ccxt` 依賴，全面改用直接 `requests` 呼叫 Binance REST API，並加入 `User-Agent` 偽裝與 SSL 動態驗證，徹底解決企業網路阻擋導致資金費率與未平倉量（OI）抓取失敗/顯示假數據的問題。
- **perf(market_data)**: 為 `yfinance` 建立自訂 Session 與 Header 偽裝，降低在 Streamlit Cloud 遭 Yahoo 阻擋的機率。

### v2.1 (2026-02-26)
- **feat(scripts)**: 新增 `scripts/daily_line_notify.py` 與 `scripts/test_flex_message.py`，實作高質感 LINE Flex Message 決策視覺化面板。
- **feat(github)**: 新增 `.github/workflows/daily_line_notify.yml` 排程，支援台灣時間 09:23 與 15:39 每日雙時段自動推播。
- **feat(market_data)**: 推播腳本內建 Kraken API 穿甲彈備援機制，並支援動態收盤價覆寫以確保指標精準度。

### v2.0 (2026-02-25)
- **perf(service)**: `@st.cache_data(ttl=86400)` SQLite 快取，與 T 日數據縫合技術消除均線斷層。
- **feat(app)**: 新增「今日大盤速覽 (Global Overview Panel)」，包含 6 大全域指標 Metric。
- **feat(handler)**: 合併原 Tab 1 與 Tab 5 為全新「🧭 長週期週期羅盤」，並精簡側邊欄，將策略參數移至各 Tab 內嵌。
- **feat(backtest)**: Tab 4 新增「🔬 最佳參數搜尋 (Grid Search)」，支援最高勝率/ROI 優化目標。

### v1.8 (2026-02-25)
- **feat(core)**: 實作四季理論目標價預測引擎，結合減半週期、倍數遞減模型與冪律走廊。

### v1.7 (2026-02-24)
- **feat(collector)**: 建立本地端 BTC 15m K 線收集器與多年度 SQLite 分割架構，實作第零層數據備援。

### v1.6 (2026-02-24)
- **feat(market_data)**: 新增 CryptoCompare 為第四層歷史數據備援。
- **feat(swing)**: Antigravity 策略升級至 v4，加入 MACD 與 ADX 過濾盤整假信號。

### v1.5 (2026-02-23)
- **perf**: 實作全面 SSL 繞過、非同步 API 請求、SQLite WAL 模式、向量化回測等多項效能優化。
