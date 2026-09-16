---
paths:
  - "service/**"
  - "core/data_sources.py"
  - "core/http_client.py"
  - "collector/**"
  - "tests/test_market_data.py"
  - "tests/test_cache_consistency.py"
---

# Cow：service 層與資料讀取陷阱

> 自 `CLAUDE.md` 逐字搬出（2026-09-15，L1 瘦身）。編號沿用原編號，外部一律引標題；標題索引在 CLAUDE.md〈已知陷阱〉。

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

### 4. service 層來源追蹤慣例

→ 已併入〈service 層 fallback chain〉。**編號保留占位，勿重編。**

### 6. 資金費率即時備援鏈

Binance `fapi/v1/premiumIndex`（`lastFundingRate`）→ Bybit `v5/market/tickers`
（`result.list[0].fundingRate`）→ OKX `api/v5/public/funding-rate`（`data[0].fundingRate`），皆 ×100。

### 16. 檔案頂層 import 會拖進重依賴

`service/macro_data.py` 頂層 `import streamlit`+`yfinance`，但 `get_next_macro_event()` 只讀本地
JSON。**只要 import 那個檔案就會連帶 import streamlit**，公司網路下這個 import 動作本身會卡住
逾時（實測 >10 秒，**try/except 攔不到「卡住」**）。已抽出零依賴的 `service/macro_events.py`。
**教訓**：新增純邏輯函式前，先看它要放的檔案頂層 import 了什麼。

### 17. `_df_from_sqlite` 曾強制欄名全轉小寫

只為相容 yfinance `'Date'`/`'date'`，卻把 `fundingRate` 這種**資料欄**也轉小寫 → 消費端讀不到、
增量 concat 產生大小寫分裂雙欄、`to_sql` 因 SQLite 欄名大小寫不敏感 `duplicate column name` 崩潰。
修法：**只在 `index_col` 找不到時**才嘗試不分大小寫改名。
詳 `_governance\歷程\20260705audit_data-internals-batch4.md` C-17~C-19。
