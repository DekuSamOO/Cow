---
paths:
  - "watcher.py"
  - "service/ohlc_universal.py"
  - "service/tw_chip.py"
  - "core/relative_*_tw.py"
  - "core/relative_*_us.py"
  - "core/relative_universal.py"
  - "scripts/tw_*.py"
  - "scripts/us_*.py"
  - "scripts/stock_profile.py"
  - "tests/test_tw_chip*.py"
  - "tests/test_watcher_stability.py"
---

# Cow：台股／美股 watcher 與量能分位陷阱

> 編號是穩定 ID（外部一律引標題）；標題索引在 CLAUDE.md〈已知陷阱〉。

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
