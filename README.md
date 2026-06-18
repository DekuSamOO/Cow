# Cow — 比特幣投資戰情室 v3.17

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
config.py           集中設定（均線週期、交易成本、倉位風控參數、WALK_FORWARD_EXIT_MODES、警報門檻/分級/遲滯常數、底部模型演算法參數：可靠度權重/礦工電價/效率 anchors 等單一可調來源）
data_manager.py     根層級數據管理器（TVL/穩定幣/資金費率歷史 SQLite 快取、指數退避重試、增量模式）
BTC_WATCH.py        BTC 雙向監控終端儀表板**正本**（2026-06-10 起由 Crypto repo 移入本 repo 維護）：純幣安 fapi/dapi + path import core 的逃頂五維/抄底六維/趨勢方向四維評分，60 秒刷新。頂部「操作訊號（三軸融合）」banner 由 composite_signal 算出（三軸皆有才顯示）。`BitcoinMonitor` 已參數化（symbol/coin_symbol/is_btc/top_cap/low_cap/title/oi_unit/nav，全部預設 BTC 向後相容）：非 BTC 時停用 ETF/SOPR/BTC.D/四季論/礦工/冪律等 BTC 專屬維度、地板改 Mayer 估值底；nav=True（由 watcher 進入）時 interruptible_wait 偵測鍵盤 b 回上層／q 結束（單獨執行 nav=False 純 sleep，行為不變）
watcher.py          通用標的監控入口：`python watcher.py` 輸入代號 → classify_symbol 自動判市場路由 —— BTC→完整 BitcoinMonitor；其他幣對→參數化 BitcoinMonitor(is_btc=False, top_cap=68/low_cap=72) 跑逃頂/抄底；美股/台股→UniversalMonitor（通用軸：趨勢方向±100＋技術＋短線動能＋趨勢×短線操作訊號 banner，日線每小時快取）。main 為 while 迴圈（儀表板內 b 重選代號／q 結束）；畫框/面板/操作訊號 helper（_panel_stance 等）重用 BTC_WATCH 單一來源

collector/
  btc_price_collector.py  本地端 15m K 線收集器（Binance + Kraken 雙源，年度 SQLite 分割，支援 --push；每日市場快照末順手強制刷新 db/etf_flow.json，git_push 一併提交）

db/                 年度分割 SQLite 資料庫（本地收集後 push 至雲端，Streamlit 直接讀 repo 內 db）
  btcusdt_15m_2013.db
  ...
  btcusdt_15m_2026.db

core/
  indicators.py       技術指標 + AHR999 計算（純函數，無 Streamlit 依賴）
                      AHR999 使用 Giovanni Santostasi 冪律：10^(-17.01467 + 5.84×log10(days))
  bear_bottom.py      熊市底部 8 大指標評分引擎 + -100~+100 牛熊複合評分（含 breakdown 分解）
  season_forecast.py  四季理論目標價預測引擎（v1.4：熊底 bottom_mult 改週期趨勢外插，抽 project_bear_bottom 為熊市分支單一來源）
  bottom_floors.py    最低價綜合評估「單一真實來源」compute_all_bottom_estimates（四季論趨勢底 + 4 floor + 鏈上錨 + 技術錨；LINE 推播與 dashboard 共用）
  miner_cost.py       礦工成本純數學模型（btc_per_day 依減半切換、eff_jth 分段插值、電費盈虧/all-in 成本，無 IO 依賴）
  gemini_client.py    Gemini REST API 輕量封裝（關閉 thinking budget 省 token、x-goog-api-key header，供新聞中文化）
  divergence.py       價格 vs 動能（RSI/MACD）頂/底背離偵測（純 pandas/numpy，無 Streamlit 依賴；detect_top/bottom_divergence_combo 供逃頂與抄底雷達共用）
  relative_high.py    相對高點（逃頂雷達）單一真實來源：Layer A 五維逃頂評分(0-100，合約/技術/鏈上/情緒/總經) + Layer B 長週期大頂 + 高點價位錨；常數 WEIGHTS/FUNDING_ANN_YELLOW(過熱起點 30%)/FUNDING_ANN_RED(滿分線 50%，2026-06 以幣安資費史回歸重校) 供 BTC_WATCH path import，無 Streamlit 依賴
  relative_low.py     相對底部（抄底雷達）單一真實來源：六維抄底評分(0-100，長週期深跌25/合約超冷20/技術回穩20/情緒恐慌15/鏈上10/總經10，權重經 relative_low_backtest 拍板)；compute_relative_low_score/relative_low_meta 供 BTC_WATCH path import，無 Streamlit 依賴
  trend_direction.py  趨勢方向（波段雷達第三軸）單一真實來源：四維**有號**評分（均線結構±40/MACD±30/斜率±15/ADX±15）→ 淨分 [-100,+100]，ADX<20 方向三維打 0.6 折防盤整假突破；compute_trend_score/trend_meta/compute_trend_direction 供 dashboard/BTC_WATCH/LINE 共用，無 Streamlit 依賴
  radar_replay.py     三雷達歷史每日分數回放（逐日重放逃頂/抄底/趨勢分數，DIV_WINDOW 視窗切片避免 O(n²)）+ threshold_forward_stats（門檻向上跨越事件 → 其後 60 日報酬分布，±18% 命中定義與權重擬合一致、cooldown 防重複計數）；僅用歷史可得輸入（OI/ETF/SOPR/BTC.D/總經與線上灰燈一致給 0 → 分數為保守下界），無 Streamlit 依賴
  action_ensemble.py  三軸合成行動建議單一真實來源 compute_composite_action（趨勢方向 × 逃頂 × 抄底 → 11 種行動 + 建議倉位區間【專家設定，未擬合】）；dashboard 與 LINE 推播共用，邊界與 trend_meta/escape_top_meta/relative_low_meta/ESCAPE_ALERT_THRESHOLD 對齊，無 Streamlit 依賴
  composite_signal.py 監控終端用三軸融合操作訊號（純函數、零網路）：compute_composite_signal（逃頂×抄底×趨勢→5 態 stance：估值到底·等止穩/分批進場/減碼出場/順勢持有/觀望，切點全沿用各軸既有等級 TREND_STRONG_BEAR/BULL ±50/+20、TOP_OVERHEAT/LOW_UNDERVALUED 60、CYCLE_DEEP_VALUE 22，不新增門檻擬合）；compute_trend_stance（股票/無衍生品標的精簡版：趨勢×短線動能）。供 BTC_WATCH/watcher 終端 banner 共用

service/
  local_db_reader.py  讀取本地 SQLite（15m 原始 / 重採樣日線），TTL 快取，全面 UTC 時區
  market_data.py      BTC / DXY 歷史數據（五層備援：本地DB→Yahoo→Binance→Kraken→CryptoCompare）
                      + T 日數據縫合，全面 UTC 時區（修正 UTC+8 偏移 8 小時 bug）
  onchain.py          鏈上輔助數據（非同步 httpx 並行，TVL/穩定幣/資金費率歷史）
  realtime.py         即時報價（Binance→Kraken→本地DB 三層備援）
                      含資金費率/OI（Binance→Bybit→OKX 三層），Header 偽裝與 SSL 繞過
                      各欄位追蹤 price_source / funding_rate_source / tvl_source
                      fetch_fng_history()：F&G 全史（alternative.me limit=0），供雷達回放與權重回測共用
  macro_data.py       宏觀數據（FRED M2/CPI/PCE/非農/失業率、Yahoo 日圓、CoinGecko BTC.D、量子威脅）+ 全面靜態備援 _FALLBACK 字典 (v1.1)；FRED CSV 改取首欄當日期欄（修 observation_date 改名後靜默走備援的 bug）
  mock.py             代理指標與模擬數據（API 失敗降級備援）
  overview.py         今日大盤速覽指標降級解析（主流程與 fragment 共用 helper，含 funding/tvl is_real 旗標）
  news.py             加密新聞多來源聚合去重（CryptoCompare/Cointelegraph/CoinDesk/Decrypt + CoinGecko 24h 熱搜）；_is_btc_crypto 嚴格過濾，只留比特幣/加密大盤、剔除山寨幣個別新聞
  news_i18n.py        Gemini 批次中文化（標題＋小結＋情緒）+ db/news_i18n.json 持久化快取（翻過不重翻）
  bottom_metrics.py   鏈上底部錨指標（bitcoin-data.com：Realized/Balanced/CVDD/MVRV-Z/SOPR）+ blockchain.info 歷史算力；429 長退避 + 12h json 快取，純資料層
  market_snapshot.py  每日市場快照（OI U本位+幣本位加總、BTC.D、資金費率、價格落地 db/market_snapshot.json）；自建合約/情緒歷史供逃頂雷達算 OI 分位/BTC.D 趨勢，純資料層
  etf_flow.py         美國現貨 BTC ETF 每日淨流量真實值（Farside read_html 解析）；抓得到更新 db/etf_flow.json，403 時回退快取（雲端讀 repo 內 db pattern），供逃頂「鏈上派發」維度；summary 含 stale_days（最新一筆距今天數，>4 天 Flex 顯示資料過舊警示）
  ohlc_universal.py   通用 OHLC 資料層（watcher.py 與 scripts/universal_watch_poc.py 共用單一來源）：classify_symbol 自動判市場（純數字4-6碼→台股.TW／含USDT/USD→幣安幣對／其餘→美股，BTC 各寫法標 is_btc=True）；fetch_ohlc 直連 Yahoo v8 chart JSON（不用 yfinance 套件，避公司 IP 429 + crumb/SSL），同端點吃幣對/美股/台股日線
  notification/       LINE/Telegram 推播模組（core 發送、builders 組 Flex：每日決策面板/逃頂警報分級配色/🎯 今日行動行/ETF 過舊警示/40KB 大小防線、facade 對外介面）

strategy/
  swing.py              Antigravity v4.1 波段策略引擎（防先視偏誤、日頻 Sharpe、多週期回測引擎）
  walkforward_backtest.py  Walk-Forward 無先視回測器（逐日推進、六層出場機制、績效統計）
  dual_invest.py        雙幣期權策略引擎（Black-Scholes，動態無風險利率）
  notifier.py           LINE Bot 主動推播通知模組

scripts/
  daily_line_notify.py     GitHub Actions 雲端自動推播腳本（Kraken 備援，台灣 08:23 / 13:39 / 18:27 三時段，含新聞輿情、逃頂警報分級/分數Δ/遲滯狀態機、OI 快照過期警告、週日傍晚場次加推文字週報）
  price_alert.py           GitHub Actions 每小時價格警報（防守線 $54k＝config.ALERT_PRICE_LOW 單一來源，含同日去重 + armed 遲滯：跌破推一次、回升門檻+$500 才重新武裝）
  test_flex_message.py     本地端測試 LINE Flex Message 排版的除錯腳本
  test_compare_backtest.py 驗證腳本：對相同參數同時執行 swing.py 與 Walk-Forward，確認結果量級一致

handler/
  layout.py          頁面設置、側欄（只保留日期區間，策略參數移至各 Tab）、行動裝置窄幅 CSS（≤880px 欄位換行＋字級縮小）
  tab_macro_compass.py Tab 1：長週期羅盤（雙 Gauge + 評分公式 expander + 三層框架 + 底部 8 指標 + 四季季節徽章/時間軸 + D2 底部支撐綜合評估（明細表收進 expander）+ D3 目標價走勢圖 + 波段雷達三軸：趨勢方向橫幅/逃頂與抄底分頁/三軸合成今日行動橫幅，與 LINE 推播同源 core）
  tab_swing.py       Tab 2：波段狙擊（3 行式 K 線子圖、2x3 條件儀表板、動態建議、倉位計算）
  tab_dual_invest.py Tab 3：雙幣理財（行權價梯形視覺化）
  tab_backtest.py    Tab 4：時光機回測（6 個子 Tab：波段 PnL、雙幣滾倉、牛市雷達、多週期回測、Walk-Forward 無先視、波段雷達回放）

tests/
  test_bear_bottom.py   熊市底部指標單元測試
  test_dual_invest.py   雙幣期權策略單元測試
  test_market_data.py   數據來源與備援鏈單元測試
  test_news.py          新聞聚合/去重/情緒彙總/中文化降級單元測試（monkeypatch 不打真 API）
  core/test_bottom_floors.py  最低價綜合評估離線單元測試（礦工成本/趨勢外插/final_low/可靠度加權中位數 ensemble/_weighted_median + 權重敏感度與 config 單一來源，注入 onchain/hashrate，10 passed）
  core/test_divergence.py     頂/底背離偵測單元測試（合成雙峰資料，確定性，4 passed）
  core/test_relative_high.py  相對高點逃頂評分單元測試（年化資費/極端高分/平靜低分/缺料 graceful/維度上限/價位錨排序，8 passed）
  core/test_trend_direction.py 趨勢方向評分單元測試（權重總和/強多/強空/盤整折扣/clamp/缺料 graceful/介面 shape，8 passed）
  bottom_floors_backtest.py   最低價地板回測（2015/2018/2022 熊底 vs 礦工電費/all-in 驗證）
  relative_high_backtest.py   逃頂權重敏感度分析（分層 train/test，AUC 以 Mann-Whitney U；僅擬合資費/技術/F&G 三維，OI/ETF/總經維持專家權重）
  relative_low_backtest.py    抄底權重敏感度分析（鏡像逃頂版；swing low+60日反彈≥18% 為正樣本，擬合負費率/技術/F&G/長週期四維，grid 過擬合→採專家配重。長週期深跌 AUC 0.662 最強）
  funding_threshold_calib.py  資費門檻校準（離線手動跑，非 pytest）：以幣安資費史(2020-12+, ~2000日)回歸，各年化資費桶→其後60日最大回撤/反彈 + 頂/底單維 AUC + Youden 門檻，重訂逃頂正費率/抄底負費率給分階梯
  test_alert_logic.py         逃頂警報分級/去重/遲滯與分數Δ狀態機測試（monkeypatch 攔截 LINE 發送，8 passed）
  test_flex_size.py           build_flex_message 40KB 大小防線、OI 快照過期/ETF 過舊警告與今日行動行測試（5 passed）
  core/test_radar_replay.py   三雷達歷史回放單元測試（合成資料：序列因果性/F&G 與資費注入/門檻事件統計，4 passed）
  core/test_action_ensemble.py 三軸合成行動決策矩陣測試（多空盤整分流/11 行動分支/邊界與警報門檻對齊/None 缺料處理，14 passed）
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

包含 6 個子分頁，提供從簡單波段到嚴格防先視偏誤的完整回測工具箱。

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

#### 子分頁 6：📡 波段雷達回放
逐日重放逃頂/抄底/趨勢方向的歷史分數序列（與 dashboard / LINE / BTC_WATCH 同源 `core` 邏輯）：
* 選雷達 + 回放年數（2-10 年）按鈕觸發，session 快取同設定只算一次（4 年約 2-3 秒）
* 價格（log）疊分數雙列圖；趨勢雷達另出多空佔比統計
* **門檻跨越事件統計**：分數向上跨越 45/60/75 → 其後 60 日報酬分布與命中率（±18% 命中定義與權重擬合一致），作為未來重校 `ESCAPE_ALERT_THRESHOLD` 的依據
* 回放僅用歷史可得輸入（技術/長週期全期、資金費率 2021+、F&G 2018+；OI/ETF/SOPR/BTC.D/總經灰燈給 0）→ **分數為保守下界**（可得天花板：逃頂 55、抄底 65）

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
| M2 / CPI / PCE / 非農 / 失業率 | FRED 公開 CSV API | 宏觀流動性與通膨/就業指標 |
| 日圓匯率 | Yahoo Finance → FRED | USD/JPY |
| BTC 市值佔比 (BTC.D) | CoinGecko /global | 山寨輪動/資金末端訊號 |
| 鏈上 SOPR | bitcoin-data.com | 獲利了結/巨鯨派發（逃頂鏈上錨） |
| 美國現貨 BTC ETF 淨流量 | Farside Investors | 機構資金進出（逃頂鏈上派發維度，db 快取回退） |
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

**失敗告警：** `daily_line_notify.yml` 與 `price_alert.yml` 任一步驟失敗時，會以 `if: failure()` step 直接 curl 推送 LINE 文字告警（含 run 連結），避免排程靜默失敗無人知；每日 Flex 卡片另在本機 OI 快照 >2 天未更新時顯示「⚠️ OI 快照已 N 天未更新」健康警告。

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

### v3.17 (2026-06-18)
- **feat(watch)**: 新增「通用標的監控」—— `watcher.py` 入口輸入代號後自動判市場路由：BTC→完整 BitcoinMonitor、其他幣對→參數化 BitcoinMonitor(is_btc=False, top_cap=68/low_cap=72) 跑逃頂/抄底但停用 BTC 專屬維度、美股/台股→`UniversalMonitor`（僅通用軸：趨勢方向±100＋技術＋短線動能）。新增 `service/ohlc_universal.py`（classify_symbol 判市場 + fetch_ohlc 直連 Yahoo v8 chart，避 yfinance 公司 IP 429）與 PoC `scripts/universal_watch_poc.py`。`BTC_WATCH.BitcoinMonitor` 參數化（symbol/coin_symbol/is_btc/top_cap/low_cap/title/oi_unit 全部預設 BTC 向後相容）。
- **refactor(simplify)**: PoC 移除自帶的 `_short_momentum`/`_bar_signed` 改 import BTC_WATCH 單一來源（消兩邊漂移、刪未用 `math`）；`UniversalMonitor` 日線改每小時快取（比照 BitcoinMonitor，避 60s 迴圈重抓 2y OHLC＋重算指標）；`watcher.main` 收斂重複的 `KIND_LABEL.get`。
- **feat(watch)**: 新增 `core/composite_signal.py`（三軸融合操作訊號，純函數零網路）：`compute_composite_signal`（逃頂×抄底×趨勢→5 態 stance，切點全沿用各軸既有等級不新增門檻擬合）＋ `compute_trend_stance`（股票精簡版：趨勢×短線動能）。`BitcoinMonitor.render` 頂部加「操作訊號（三軸融合）」banner（三軸皆有才顯示）、`UniversalMonitor.render` 加「操作訊號（趨勢×短線）」banner。儀表板內按鍵導覽：`interruptible_wait` 偵測 b 回上層／q 結束（`BitcoinMonitor(nav=...)`，nav=False 純 sleep 向後相容）；`watcher.main` 改 while 迴圈。新增離線回測 `scripts/backtest_composite.py`（2 年 composite 前瞻報酬）、`scripts/backtest_radar_at_date.py`（指定日期回算逃頂/抄底）。
- **refactor(simplify)**: 抽 `BTC_WATCH._panel_stance` 收斂 BitcoinMonitor/UniversalMonitor 重複的「stance→banner」格式化（gate 各自保留）；`composite_signal` 補 `TREND_STRONG_BULL` 常數取代裸字面；`backtest_radar_at_date` F&G 改 `fetch_fng_map` 在 main 抓一次傳入（消多日期重複下載）。

### v3.16 (2026-06-17)
- **feat(radar)**: 資費門檻以幣安資費史回歸重校（取代鎖在罕見極值的舊階梯）。新增 `tests/funding_threshold_calib.py`（離線手動跑，非 pytest）：以幣安資費史(2020-12+, ~2000日)回歸，各年化資費桶 → 其後 60 日最大回撤/反彈 + 頂/底單維 AUC + Youden 門檻。關鍵發現——後 60 日回撤在年化 ≥30% 由 ~-10% 翻倍至 ~-18%（轉折）、≥50% 飽和、≥70% 未更深；年化 ≥90% 僅 35 日(1.76%)全在 2021 狂熱且不更準。負費率判別力在「淺負」(Youden 最佳 ≤-3%)；≤-15/-20/-30% 僅 8/4/3 日(危機)、召回崩到 3%。
- **feat(core)**: `core/relative_high._score_derivatives` 逃頂正費率階梯重校為 `≥50→20 | ≥40→17 | ≥30→14 | ≥20→6 | ≥12→2`（滿分線 90%→50%、過熱起點 50%→30%）；常數 `FUNDING_ANN_YELLOW` 50→30、`FUNDING_ANN_RED` 90→50。`core/relative_low._score_derivatives_low` 抄底負費率階梯重校為 `≤-20→10 | ≤-10→8 | ≤-5→6 | ≤-2→3 | <0→1`（判別帶下移至淺負）；常數 `FUNDING_ANN_LOW_YELLOW` -15→-5、`FUNDING_ANN_LOW_RED` -30→-20。三端（dashboard / LINE 推播 / BTC_WATCH）path import 同源自動同步。
- **refactor(test)**: `tests/relative_high_backtest.py::funding_score` 階梯與正本 `_score_derivatives` 對齊（消除常數語意翻轉後遺留的兩處死碼），維持「與 core 同邏輯」承諾、杜絕兩邊閾值漂移。
- **test**: 全套 pytest 142 passed（2 yfinance 為環境性 Yahoo 阻擋，非本次改動）。

### v3.15 (2026-06-11)
- **feat(dashboard)**: Tab 1 資訊架構收合 —— 波段雷達逃頂/抄底兩區改 `st.tabs` 分頁（趨勢橫幅與三軸合成橫幅在分頁外恆顯）；D2 底部支撐「各算法明細表」收進 `st.expander`（總結兩卡維持可見）。
- **feat(dashboard)**: 核心區塊（趨勢/逃頂/抄底/四季雷達/底部資料）計算失敗時由 `st.caption` 升級 `st.warning`，統一走 `_warn_unavailable` helper（保留例外型別＋訊息）；次要備註維持 caption。
- **refactor(dashboard)**: `tab_macro_compass.render` 簽名 14 → 10 參數 —— 5 個速覽解析值（funding_rate/tvl/fng×3，來自 `service/overview.resolve_overview_metrics`）合併為 1 個 `ov`（OverviewMetrics），render 開頭 unpack 回既有變數名；`app.py` 呼叫端同步並移除 4 個死變數。
- **feat(ui)**: `handler/layout.py` CUSTOM_CSS 加 `@media (max-width: 880px)` —— 5-6 欄區塊允許換行（min-width 150px）＋ metric 字級縮小，改善行動裝置可讀性（視覺效果待手機實測）。
- **feat(notify)**: 週日 LINE 週報 —— `maybe_send_weekly_summary` 於台灣週日 ≥17 時場次（18:27）加推一則文字週報（週價格區間/漲跌 + 逃頂/抄底分數週高低 + 趨勢 + 行動），state `last_weekly_date` 每週去重、資料不足自動跳過；`get_decision_data` 增算近 7 日 week_high/week_low/week_change_pct；`attach_score_deltas` score_history 保留 3 → 8 天（Δ 用昨日、週報用整週；artifact retention 2 天但每日 3 次 run 重新上傳，鏈不會斷）。
- **test**: `tests/test_alert_logic.py` 新增 3 個週報測試（非週日/週日早上不發、週日傍晚發＋內容與去重斷言、資料不足跳過），11 passed。

### v3.14 (2026-06-10)
- **feat(core)**: 新增 `core/radar_replay.py`（三雷達歷史每日分數回放：escape/low/trend score series，DIV_WINDOW 視窗切片避免 O(n²)；`threshold_forward_stats` 門檻向上跨越事件 → 其後 60 日報酬分布，±18% 命中定義與權重擬合一致 + cooldown 防重複計數）；`service/realtime.py` 新增 `fetch_fng_history()`（F&G 全史，收斂原本散落兩個回測腳本的 ad hoc 抓取）。
- **feat(dashboard)**: 回測 Tab 新增第 6 子分頁「📡 波段雷達回放」（`handler/components/backtest_radar.py`：選雷達+年數按鈕觸發、session 快取、價格疊分數雙列圖、門檻統計表、趨勢多空佔比）。
- **feat(core)**: 新增 `core/action_ensemble.py`（三軸合成行動建議單一真實來源：趨勢方向 × 逃頂 × 抄底 決策矩陣 → 11 種行動 + 建議倉位區間【未擬合】，邊界與各雷達 meta/警報門檻對齊）；dashboard 波段雷達加三軸合成行動橫幅（三個 `_render_*` 改回傳計算結果）、LINE Flex 波段雷達 box 加「🎯 今日行動」行（無資料自動隱藏）。
- **refactor(config)**: 底部模型 magic numbers 升 `config.py` 新區段（`BOTTOM_RELIABILITY`/`MINER_BOTTOM_MULT`/`MAYER_BOTTOM_RATIO`/`AHR999_DCA_CEIL`/`MINER_ELECTRICITY_RATE`/`MINER_ALLIN_FACTOR`/`MINER_EFF_ANCHORS`）；`core/bottom_floors.py`、`core/miner_cost.py` 改 import 並保留既有內部別名 → 下游（BTC_WATCH/推播）零改動。四季論各輪 mult 為歷史實測值，刻意留在 `core/season_forecast`。
- **feat(etf)**: Farside ETF 資料防護 —— collector 每日快照末強制刷新 `db/etf_flow.json`；`get_etf_flow_summary` 新增 `stale_days`，>4 天時每日 Flex 顯示「⚠️ ETF 流量資料為 N 天前」（週末 1-3 天空窗不警示）。
- **fix(core)**: `core/bear_bottom.py` 全部 SMA（111/350/365/730/1400）與月線 RSI 套 `core/indicators._ta_series`，修資料不足時 pandas-ta 回 `None` 導致 `.reindex` AttributeError 崩潰（A1 同型 bug）。
- **test**: 新增 `tests/core/test_radar_replay.py`（4 個）、`tests/core/test_action_ensemble.py`（14 個）；`test_bottom_floors.py` 加權重敏感度與 config 單一來源測試（10 passed）；`test_flex_size.py` 加 ETF 過舊與今日行動行測試（5 passed）。

### v3.13 (2026-06-10)
- **feat(alerts)**: 防守警報門檻 $58,000 → **$54,000**（`config.ALERT_PRICE_LOW`，對齊 BTC_WATCH 防線 fallback）。
- **refactor(alerts)**: 門檻命名去硬編碼 —— `notify_58k_defense` 改名 `notify_defense_line`，推播標題動態帶入 `ALERT_PRICE_LOW`；`scripts/price_alert.py` state 鍵改 `last_defense_date` / `armed_defense`（舊 58k 鍵作廢，缺鍵視為已武裝屬預期）。下次調門檻只需改 config 一行。

### v3.12 (2026-06-10)
- **fix(core)**: `core/indicators.py` 新增 `_ta_series()` helper —— pandas-ta 0.4.x 在資料長度不足（如 <200 根算 SMA200）時 `ta.sma/ema/rsi/atr` 回傳 `None`，導致下游 `.diff(20)` / `.reindex` 崩潰，統一轉為 NaN Series；`tests/core/test_indicators.py` 修正斷言 typo（SMA_20 → SMA_200）並加強短資料斷言。
- **feat(actions)**: `daily_line_notify.yml` / `price_alert.yml` 各加 `if: failure()` 的 LINE curl 告警 step，排程失敗不再靜默；`service/market_snapshot.py` 新增 `get_snapshot_staleness_days()`，每日 Flex 在本機 OI 快照 >2 天未更新時顯示健康警告。
- **feat(alerts)**: 逃頂警報分級（60 預警 / 75 警報 / 85 危急，`config.ESCAPE_ALERT_TIERS`）—— header 標題/底色依分級切換、顯示「較上次警報 +N 分」；`maybe_send_escape_alert` 重寫為狀態機：同日去重保留 + 跨日需 +5 分或升級才再推 + 低於門檻解除武裝；每日 Flex 波段雷達加逃頂/抄底分數 vs 前一推播日 Δ（`attach_score_deltas`，score_history 留近 3 日，與警報共用 `escape_alert_state.json` artifact）。
- **feat(alerts)**: `scripts/price_alert.py` $58k 防守警報加 armed 遲滯 —— 跌破推一次即解除武裝，回升至門檻+$500（`ALERT_PRICE_REARM_GAP`）才重新武裝，防門檻附近震盪隔日反覆推播。
- **feat(notify)**: `builders.py` 加 Flex payload 大小防線（軟上限 40KB，LINE 硬上限 50KB）—— 超限先移除新聞區塊並 log。
- **refactor(BTC_WATCH)**: `BTC_WATCH.py` 硬編碼 Cow 路徑改由 `__file__` 推導（換機/搬資料夾不需改碼）。
- **test**: 新增 `tests/test_alert_logic.py`（警報分級/去重/遲滯/Δ 狀態機，monkeypatch 攔截 LINE 發送，8 個）與 `tests/test_flex_size.py`（大小防線/過期警告，3 個）。

### v3.11 (2026-06-10)
- **feat(core)**: 新增 `core/trend_direction.py`（波段雷達第三軸「趨勢方向」單一真實來源，鏡像 `relative_low.py` 結構但分數**有號**）。四維評分：均線結構 ±40（close/SMA50/SMA200 排列）/ MACD ±30（零軸×金叉死叉）/ 斜率 ±15（SMA200 標準化斜率+SMA50 近20日）/ ADX ±15（強度×前三維方向）→ 淨分 [-100,+100]；ADX<20 時方向三維打 0.6 折（盤整防假突破內生到分數）。與逃頂/抄底正交：「貴不貴」之外補「風往哪吹」，可同時「強多頭+逃頂高」或「空頭+抄底高」（勿純憑估值接刀）。
- **feat(dashboard)**: `handler/tab_macro_compass.py` 波段雷達頂部新增 `_render_trend_banner`（Plotly gauge ±100 五色帶 + 四維 metric + 操作意涵）。
- **feat(notify)**: `scripts/daily_line_notify.py` `_compute_radars` 增算 trend 五欄位；`service/notification/builders.py` 新增 `_build_trend_strip`（等級+有號分數+置中方向條，左空右多）嵌入波段雷達 box 頂部，無 `trend_signals` 時自動省略；`_dominant_dim` 泛化為取 |score|/max（逃頂/抄底分數皆 ≥0 行為不變），三雷達共用主導維度邏輯。
- **feat(BTC_WATCH)**: `BTC_WATCH.py` **正本自 Crypto repo 移入本 repo 根目錄**維護（Crypto 那份已刪除）；新增 `_bar_signed`（±100 置中條）與 `_panel_trend`（有號分數面板），主迴圈接 `compute_trend_score`，趨勢面板顯示於逃頂/抄底之上。
- **test**: 新增 `tests/core/test_trend_direction.py`（8 個確定性離線測試：權重總和 100/強多 ≥50/強空 ≤-50/盤整帶/弱趨勢折扣/clamp/缺料 graceful/介面 shape）。

### v3.10 (2026-06-09)
- **feat(core)**: 新增 `core/relative_low.py`（相對底部抄底雷達，鏡像 `relative_high.py`）。六維抄底評分(0-100)：長週期深跌 25 / 合約超冷 20 / 技術回穩 20 / 情緒恐慌 15 / 鏈上 10 / 總經 10，`compute_relative_low_score`/`relative_low_meta`/`compute_relative_low` 供 dashboard 與 Crypto/BTC_WATCH.py path import。`UNFITTED_DIMS_LOW=("onchain","macro")`、負費率閾值標未擬合。
- **feat(core)**: `core/divergence.py` 補 `detect_bottom_divergence_combo`（RSI+MACD 底背離，對稱 `detect_top_divergence_combo`），供 relative_low 技術維度。
- **test**: 新增 `tests/relative_low_backtest.py`（抄底權重敏感度分析，鏡像逃頂版）。實證：**長週期深跌 AUC 0.662 最強**（底部靠估值便宜、頂部靠槓桿過熱，兩側天生非對稱）；grid search 過擬合（36 樣本 test AUC 0.438）→ 採專家配重。負費率 AUC 0.533 最弱但 OI 清洗子項不可回測仍保留。
- **note(Crypto)**: 配套改寫 `Crypto/BTC_WATCH.py` —— OI 改 `openInterestHist` 5m×13(1h滾動)+1d×30(分位) 取代失效的相鄰 60s 差值、日線 limit→1500（200週可算）、防線改動態 `bottom_floors.final_low`（fallback 54000）、儀表板化（cls 清屏+emoji+逃頂五維/抄底六維）。可得天花板：逃頂 65、抄底 75（已接 alternative.me F&G；BTC.D/ETF/SOPR/總經無源灰燈）。

### v3.9 (2026-06-08)
- **feat(notify)**: 逃頂警報 Flex 化。`service/notification/builders.py` 新增 `build_escape_alert_flex()`，將逃頂評分 ≥ 門檻時的獨立警報由純文字改為完整 Flex bubble（紅色 header「🚨 BTC 逃頂警報」+ 與每日 Flex 同一個 `_build_escape_box` 逃頂雷達 box + 操作建議 box），缺 `escape_signals` 時回 `None`；`scripts/daily_line_notify.py` 的 `maybe_send_escape_alert` 改送此 Flex 並保留每日去重（`escape_alert_state.json`）。
- **refactor(notify)**: 抽出共用 `_build_advice_box(label, text, color)` helper，每日 Flex「策略建議」與逃頂警報「操作建議」共用同一黃底建議 box，消除複製貼上。

### v3.8 (2026-06-08)
- **feat(core)**: 新增「相對高點（逃頂雷達）」Phase 0+1 後端基礎建設（純函數/資料層，尚未接 UI）。`core/divergence.py` 價格 vs RSI/MACD 頂/底背離偵測（嚴格局部極值 + regular divergence 正規定義，純 pandas/numpy）；`core/relative_high.py` 單一真實來源——Layer A 五維逃頂評分(合約過熱 30/技術衰竭 25/鏈上派發 20/情緒過熱 15/總經逆風 10) + Layer B 長週期大頂（複用 `bear_bottom` bull_total + 四季論秋季）+ 高點價位錨（Pi Cycle 頂/Mayer 頂/冪律上界/四季論牛頂）；常數 `WEIGHTS`/`FUNDING_ANN_RED`/`UNFITTED_DIMS` 為跨 repo 單一來源（供 BTC_WATCH path import），全程零 Streamlit 依賴、零網路請求（外部資料由呼叫端注入）。
- **feat(service)**: `service/market_snapshot.py` 每日落地 OI（U本位+幣本位加總）/BTC.D/資金費率快照至 `db/market_snapshot.json`，自建合約/情緒歷史供 OI 分位與 BTC.D 趨勢計算（Binance 端點僅留 30 天、CoinGecko 無免費歷史）；`service/etf_flow.py` 以 Farside `read_html` 抓美國現貨 BTC ETF 真實日淨流量，抓得到更新 `db/etf_flow.json`、403 時回退快取（沿用「雲端讀 repo 內 db」pattern）。
- **feat(service)**: `service/macro_data.py` 補 `fetch_us_pce_yoy`/`fetch_nfp`/`fetch_unrate`（FRED PCEPI/PAYEMS/UNRATE）與 `fetch_btc_dominance`（CoinGecko /global），皆含靜態備援；`service/bottom_metrics.py` `_ENDPOINTS` 加 SOPR 端點（逃頂鏈上錨）。
- **fix(macro)**: `_fred_fetch` 修既有 bug——FRED 已將 CSV 首欄表頭由 `DATE` 改名為 `observation_date`，舊版寫死 `parse_dates=["DATE"]` 導致 CPI/M2/USDJPY 解析失敗靜默走靜態備援；改為「取第一欄當日期欄」+ 格式檢核，timeout 15→30s。
- **test**: 新增 `tests/core/test_divergence.py`（4 passed）、`tests/core/test_relative_high.py`（8 passed）共 12 個確定性離線測試；`tests/relative_high_backtest.py` 逃頂權重敏感度分析（分層 train/test，Mann-Whitney U AUC，僅擬合資費/技術/F&G 三維，OI/ETF/總經維持專家權重）。

### v3.7 (2026-06-05)
- **chore(deps)**: `requirements.txt` / `pyproject.toml` 全面鎖版本並對齊（單一真相為 requirements.txt）。關鍵約束 `numpy>=1.26,<2`——pandas-ta 0.3.x 仍 `from numpy import NaN`，numpy 2.x 會在 import 階段崩潰（本地測不出、雲端重建才爆）；`streamlit==1.37.1` 鎖雲端實跑版本避免行為漂移；其餘套件補 `>=` 下界。`pyproject.toml` 補齊 pandas-ta/yfinance/httpx/line-bot-sdk/urllib3、version 升 3.5.0、新增 `[tool.pytest.ini_options] testpaths=["tests"]`（只收集正規單元測試，排除 `scripts/test_*.py` 手動除錯工具）。
- **refactor(season)**: `core/season_forecast.py` 新增 `_utcnow_naive()` helper 取代 5 處 Python 3.12 已棄用的 `datetime.utcnow()`；`project_bear_bottom()` 新增 `market_state` 可選參數，讓 `forecast_price` 熊市分支複用已算好的 market_state，省一次 `rolling(200)`。`core/bottom_floors.py`、`handler/tab_macro_compass.py` 同步將 `utcnow()` 改 `datetime.now(timezone.utc).replace(tzinfo=None)`。
- **refactor(service)**: `service/bottom_metrics.py` 移除唯一繞過統一封裝的 `requests.get` 與自寫 429 重試迴圈，改用 `core.http_client.safe_get`（統一 UA／重試）；`utcfromtimestamp` → `fromtimestamp(tz=utc)`。
- **chore(logging)**: service 層 7 檔（bottom_metrics/notification.core/onchain/realtime/macro_data/market_data/historical_data_manager）77 處 `print` 改 `logging`，各檔加 `logger = logging.getLogger(__name__)`；僅保留 historical_data_manager 的 3 個 `__main__` banner print。
- **fix(scripts/test)**: `scripts/test_flex_message.py` 補上未定義的 `_REPO_ROOT`（既有 NameError，曾使 pytest collection 中斷）；`tests/core/test_season_forecast.py` 過時的 tuple 斷言改寫為現行 dict API。

### v3.6 (2026-06-05)
- **feat(core)**: 新增 `core/bottom_floors.py` 作為最低價評估「單一真實來源」`compute_all_bottom_estimates`，整合四季論趨勢底 + 4 個 floor（200週均線/冪律/礦工電費/礦工 all-in）+ 鏈上錨（Realized/Balanced/CVDD）+ 技術錨（Mayer 底/AHR999）；`final_low = max(四季論趨勢底, 礦工電費硬地板)`、`ensemble = 可靠度加權中位數`。
- **feat(core)**: `bottom_floors.py` ensemble 由「等權強錨中位數」升級為「可靠度加權中位數」——新增 `_RELIABILITY` 各算法可靠度權重表（realized 82 ~ miner_allin 50）與 `_weighted_median()`，每個 estimate 帶 `reliability` 欄、排除 all-in 警示線，使綜合估計落進高可靠度叢集。
- **feat(notify/dashboard)**: 配色統一——四季論趨勢底改紅、鏈上/技術錨改黃（floor 藍/warning 橙不變）；LINE 推播精簡為代表性 6 項（最高估計 + 3 硬地板 + 最低估計 + 四季論趨勢底，去重），完整 10 項移至 dashboard D2.5（新增「可靠度」欄，綠≥75/黃≥62/橙<62）。
- **feat(core)**: 新增 `core/miner_cost.py` 礦工成本純數學模型（btc_per_day 依減半切換、eff_jth 分段插值、電費盈虧/all-in），供回測與即時評估共用。
- **feat(season)**: `core/season_forecast.py` 升 v1.4——熊底 `bottom_mult` 改「週期趨勢外插」(`extrapolate_bottom_mult`) 取代 median/p25（三輪 13.1%→15.7%→22.5% 遞增、底部漸淺，留一法誤差 -19% 優於 median -30%）；抽 `project_bear_bottom()` 為 forecast_price 熊市分支與 bottom_floors 共用底部來源，杜絕兩邊漂移。
- **feat(service)**: 新增 `service/bottom_metrics.py` 鏈上底部錨指標（bitcoin-data.com）+ blockchain.info 歷史算力；429 長退避 + 12h json 快取（`db/bottom_metrics_cache.json` / `db/hashrate_history.json`，皆為執行快取不入版控）。
- **refactor(notify)**: `scripts/daily_line_notify.py` 改用 `compute_all_bottom_estimates` 為單一來源、回填舊 floor 欄位（向後相容），移除已不再使用的 `fetch_floor_indicators()` / `_miner_cost_from_ths()`。
- **feat(notify)**: `service/notification/builders.py` 新增 `_build_bottom_eval_box` 合併「最低價綜合評估」單一 block；無 `bottom_eval` 時自動 fallback 舊兩 box（保留 `_build_forecast_box`/`_build_floor_support_box`）。
- **feat(dashboard)**: `handler/tab_macro_compass.py` Tab 1 新增 D2.5「底部支撐綜合評估」區塊，與每日 LINE 推播同源。
- **feat(news)**: `service/news.py` 新增 `_is_btc_crypto` 嚴格過濾——只保留比特幣/加密大盤新聞，提到任何山寨幣（含 Bitcoin Cash/BCH）即剔除。
- **test**: 新增 `tests/core/test_bottom_floors.py`（8 passed，離線注入，含 `_weighted_median` 與可靠度加權 ensemble）與 `tests/bottom_floors_backtest.py` 回測驗證。
- **refactor(dashboard)**: `handler/tab_macro_compass.py` Tab 1 精簡 D 區段——移除四季論預測卡片（最深/熊市最低/最淺目標 3 欄）、預測信心分數 bar 與「📖 預測邏輯說明」expander（資訊已與 D2 底部支撐綜合評估重疊）；季節徽章/時間軸保留，原 D2.5 改名為 D2。
- **fix(market_data)**: `service/market_data.py` `fetch_market_data()` 回傳前新增壞時間戳安全網——濾掉早於創世日（2009-01-03）的列並回寫乾淨 CSV，自我修復縫合備援偶發的單位解析錯誤時間戳（落到 ~1969/epoch0、會把圖表 X 軸從 1969 拉到今天、擠爆熊市區塊），保護所有消費端。

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
