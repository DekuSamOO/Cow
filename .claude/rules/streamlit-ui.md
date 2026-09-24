---
paths:
  - "app.py"
  - "handler/**"
  - "service/overview.py"
  - "service/news.py"
  - "tests/test_cache_consistency.py"
---

# Cow：Streamlit／UI 陷阱

> 編號是穩定 ID（外部一律引標題）；標題索引在 CLAUDE.md〈已知陷阱〉。

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

### 5. `reindex(method='nearest')` 早於資料起點填充定值

`fund_hist` 從 2021 起、`chart_df` 從 2015 起 → 2021 年前會填成第一筆值（常數線）。
手動清除：`fund_sub.loc[fund_sub.index < fund_hist.index[0]] = np.nan`

### 7. 圖表欄位名與顯示標籤混淆

`EMA_20` 直接當圖例易被誤讀為 `SMA 20` → `_ma_label(col)` 轉 `EMA 20`。
`exit_ma_key == 'EMA_20'` 時進場線與防守線同一條，合併標籤 `"EMA 20 (進場 ＆ 防守線)"`。
