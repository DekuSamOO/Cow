---
paths:
  - "service/news.py"
  - "service/news_i18n.py"
  - "core/gemini_client.py"
  - "tests/test_news.py"
---

# Cow：新聞來源與 Gemini 陷阱

> 編號是穩定 ID（外部一律引標題）；標題索引在 CLAUDE.md〈已知陷阱〉。

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
