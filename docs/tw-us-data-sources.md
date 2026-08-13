# 台股／美股資料源實作細節（正本）

> 2026-08-05 自 `CLAUDE.md` 移出。CLAUDE.md 每個 session 都載入，端點清單與欄序索引
> 屬「詳細 API 文件」，依 Anthropic 官方 CLAUDE.md 準則應給連結而非全文常駐。
> **CLAUDE.md 只留三條會害人犯錯的行為守則**，其餘細節在本檔。

---

## No.1 TWSE：端點都是「市場全量單日檔」，非個股查詢

抓整檔每小時快取再 filter symbol。

| 用途 | 端點 | 參數 |
|---|---|---|
| 融資融券 | `marginTrading/MI_MARGN` | `selectType=STOCK` |
| 三大法人 | `fund/T86` | `ALLBUT0999` |
| 本益比／PB | `afterTrading/BWIBBU_d` | `ALL` |

`BWIBBU_d` 欄序（2026-08-12 實查 8 欄）：
`0證券代號 1證券名稱 2收盤價 3殖利率(%) 4股利年度 5本益比 6股價淨值比 7財報年/季`。
**第 8 欄「財報年/季」是 PE 的基期，語意為「近四季（TTM）EPS 截止於哪一季」**（民國制
`115/2`）——**不是**「拿那一季的 EPS 算的」，PE 的分母是四季和，本欄只標窗口的最後一季。

2026-08-13 兩檔對拍（收盤 ÷ PE ＝ 隱含 EPS，再與外部近四季 EPS 比對）：

| 代號 | 原始 | `chip.as_of` 收盤 | PE | 隱含 EPS | 四季和 |
|---|---|---|---|---|---|
| 6782 | `115/2` | 207.5 | 12.58 | 16.49 | 4.06＋3.98＋3.82＋4.63 ＝ 16.49 ✅ |
| 2330 | `115/1` | 2415.0 | 32.47 | 74.38 | 15.36＋17.44＋19.51＋22.08 ＝ 74.39 ✅ |

反證：2330 單季 EPS 22.08 年化（88.32）→ PE 27.34、單季本身 → 109.4，**都對不上**。
同一天不同股票的截止季可以不同（上表兩檔就差一季）。少了它「PE 13 不貴」查不出便宜在
哪個窗口上；循環股高峰期 TTM 只是把失真平滑一部分，**不會消除**。
由 `get_valuation` 回 `pe_fiscal_quarter`（西元 `2026Q2`）＋ `pe_fiscal_quarter_raw`（原樣）。

> [!warning] 驗算時分母要配對
> 用 `chip.as_of` 當日**收盤價**，不是 `stock_profile` 輸出的 `price`（盤中即時價，基準日不同）。
> 配錯會得到一個貌似合理的錯 EPS——實測用 `price` 207.0 會算出 16.45，而正確值是 16.49，
> 只有 16.49 能被四季 EPS 整除還原。

- 回應有時包在 `tables[]` → 需**深找含 `fields`+`data` 的 table**。
- **Accept-Encoding 勿帶 `br`**：T86 回 brotli，requests 無 brotli 套件時解碼壞
  → 固定 `gzip, deflate`。
- `MI_MARGN` 單一回應即含「前日＋今日餘額」→ 融資變化免多日累積；T86 為單日買賣超。

## No.2 TDCC 集保大戶分布：GET → POST CSRF

鏡像 tw_stock_climber 的作法：先 GET `qryStock` 抓 `SYNCHRONIZER_TOKEN`／`SYNCHRONIZER_URI`，
再 POST 帶 token ＋ `firDate`/`scaDate`（最近已公布週五，扣 7 天公布延遲）。
`pd.read_html` 解析持股分級表：大戶 ≥1000 張／中實戶 ≥400 張／散戶 ≤50 張。

**同週同檔記憶體快取不重抓；勿密集打**（每檔間需 sleep）。

**現況**：逃頂側散戶 `retail_pct` 低權保留（樣本 2023-09 起僅 2.7 年、薄）；
**抄底側大戶 `major_pct` 已於 2026-07-02 因方向反而整維移除**——是移除不是「維持低權」。
（AUC 數值一律以 `_governance\FINDINGS-cow-radar-backtests.md` No.5 為準，本檔不複述。）

## No.3 上櫃 TPEx fallback（2026-06-23）

三個日檔（估值／融資／法人）皆「上市 TWSE → 查無（上櫃股）轉打對應 TPEx 端點」。
`_fetch_market_file` 加 `base` 參數（預設 `_TWSE`，上櫃傳 `_TPEX="https://www.tpex.org.tw"`）。

- **cache key 改 `(base, endpoint, date)`** 避免同端點撞檔
- TPEx 日期皆走 `_tpex_date` 轉 `yyyy/mm/dd`

| 函式 | TPEx 端點 | 欄序 |
|---|---|---|
| `_get_valuation_tpex` | `www/zh-tw/afterTrading/peQryDate` | PE=2／殖利率=5／PB=6／**財報年/季=7**，**無收盤價欄 → `close=None`**（呼叫端勿對 close 做算術）。財報年季格式與 TWSE 不同：TPEx `115Q1`、TWSE `115/2`，兩種都由 `_roc_quarter` 吸收 |
| `_get_margin_tpex` | `www/zh-tw/margin/balance` | 2 前資餘額／6 資餘額／10 前券／14 券餘額（單位張，同 TWSE）；與 TWSE 共用 `_margin_dict` |
| `_get_institutional_tpex` | `www/zh-tw/insti/dailyTrade`（`type=Daily`） | 4 外資(不含自營)／13 投信／22 自營合計／23 三大法人合計。**與 TWSE T86 的 4/10/11/18 不同**，故兩函式分開；評分只用 `total_net` |

成效：上櫃股（6488／8069）OHLC ＋ 籌碼四維全可用，不再因上櫃灰燈；上市路徑不變。

## No.4 股本 API 兩坑（`tw_chip.get_shares_outstanding`，供週轉率）

1. **該 domain 的 requests 自動編碼偵測常猜錯** → 必須強制 `r.encoding = "utf-8"`
   （`.json()` 內部 `if not self.encoding` 才觸發 BOM 猜測，設了就走指定編碼）。
2. 回應 ~1MB+ 全市場、實測 20–45 秒 → **timeout 60s、快取 24h**（股本變動極不頻繁）；
   抓取失敗退回舊快取而非清空。

端點與實測值見 `_governance\FINDINGS-cow-radar-backtests.md` No.5.4。

## No.5 美股與通用軸

- 美股缺免費籌碼源 → 改用**純 OHLCV 通用軸**（量價背離＋結構轉折，複用 `core/divergence`）
- `core/relative_high_us.py`／`relative_low_us.py`（v0.1）——**標「僅參考、未獲實證」**
- 台股／美股共用 `core/relative_universal.py`
- **踩坑**：`us_universal_backtest._fill` 原用 `reindex` 對齊分數，跨 50 檔 concat 後
  DatetimeIndex 重複日期會崩 → 已改逐檔 positional 賦值

## No.6 配重與校準沿革

配重明細、v0.2~v0.5 校準沿革與各維 AUC、美股三維雜訊結論
→ `_governance\FINDINGS-cow-radar-backtests.md` No.5

逐輪推導見 PLAN（vault `Zettelkasten\Github\Cow\`）：
`20260621plan_股票版逃頂抄底維度.md`、`20260622plan_台股維度回測校準.md`、
`20260626plan_台股維度v0.3弱維強化.md`、`20260702plan_通用量價結構訊號與美股框架.md`。

## No.7 其他

Cow **不 import tw_stock_climber**（保持自包含、雲端可跑），僅鏡像爬法／分級概念。

## No.8 每月營收（`tw_chip.get_monthly_revenue`，2026-08-12 新增）

MOPS 月營收彙總，與股本同 OpenAPI 家族（**不是** No.1 的 `rwd/zh` 系列）。

| 市場 | 端點 | 實測筆數（2026-08-12） |
|---|---|---|
| 上市 | `https://openapi.twse.com.tw/v1/opendata/t187ap05_L` | 1,069 |
| 上櫃 | `https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O` | 890 |

**兩端點 JSON key 完全相同** → 共用一套解析（`_fetch_revenue_market`），上市查無再轉上櫃。
沿用 No.4 股本 API 的同兩坑：強制 `r.encoding = "utf-8"`、回應大故 timeout 90s、日快取、
抓取失敗退回舊快取。

> [!warning] **無 date 參數 → 只有最新一期，沒有歷史序列**
> 因此月營收**只能當描述性事實，不得餵進逃頂/抄底雷達或任何評分**：沒有歷史就跑不出回測，
> 未回測的維度加權成分數會產生虛假的驗證感（CONSTITUTION 第 8-12 條）。
> 這也是 `stock_profile` 把它放在 `out["revenue"]` 而**不是** `out["chip"]` 的原因——
> `chip` 會整包被 `compute_relative_*_tw` 吃進去評分，月營收不該進那條路。
> 要做序列得自行逐月歸檔累積，那是另一件事。

單位：金額欄原始即為**仟元** → key 一律帶 `_ktwd` 後綴（千倍級單位不寫進欄名遲早被當成元）。
成長率**照抄來源的 `(%)` 欄**，不由三個金額回推（來源自算的才是官方口徑）。
兩個日期都要標：`data_month`（資料年月）與 `published_at`（出表日期，PiT 用）——
只標資料月份會讓人以為月底就拿得到，實際隔月 10 號前才出表。

民國轉換由 `_roc_quarter`／`_roc_ym`／`_roc_ymd` 三支負責，解析不出來一律回 `None`（不猜），
raw 值另存欄位。西元格式（如 `2026Q2`）會被正確拒絕，不會誤判成民國。
