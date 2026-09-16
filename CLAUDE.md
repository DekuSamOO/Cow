# CLAUDE.md — Cow（BTC 投資戰情室）

**路徑：** `D:\Users\63191\Documents\GitHub\Cow`　**Live：** https://mfyyo9qf5mymsrouxkfdgj.streamlit.app

> **📌 否決史唯一正本**：vault `Literature Note\4b Cow 開發決策史.md` 第三節。
> 本檔與 AUDIT/STRESS 各檔的否決敘述僅為摘要；**復活禁令查核一律查該表，新否決先入該表再引用**。
> **架構決策** → `_governance\ADR\Cow\`（ADR-001~004）
> **子系統規格** → `_governance\SPEC\cow-radar-spec.md`（每季隨 PREREG §0 第 5 點 (d) 校準）
> **回測數據正本** → `_governance\FINDINGS-cow-radar-backtests.md`
> **台股/美股資料源細節** → `docs\tw-us-data-sources.md`
> 版本與部署狀態以 README 為準，本檔不複述。
> **專題陷阱 → `.claude/rules/`**（2026-09-15 起；官方 path-scoped rules，**Read 到對應檔才自動載入**，
> 不佔每次對話的 context）。本檔〈已知陷阱〉留標題索引，外部引用照舊引標題即可找到。

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

## 三大核心系統（改動守則；實作細節在 rules）

- **單一真實來源，勿各自重算**：最低價＝`core/bottom_floors.py::compute_all_bottom_estimates()`；
  逃頂／抄底／趨勢＝`core/relative_high.py`／`relative_low.py`／`trend_direction.py`，dashboard、`BTC_WATCH.py`、LINE 推播共用。
- **三軸要合看**：可同時「強多頭＋逃頂高」或「空頭＋抄底高」——**勿純憑估值接刀**。
- **⚠️ OI×Funding 假頂折減仍 NOT VERIFIED**（樣本不足非網路問題）：只准折減不准灌分、
  OI 無資料不折減。**不得移除、也不得轉正**。
- **反指標整段移除、不留參考顯示**（Hash Ribbons、TDCC major_pct 前例）。勿加回。
- 最低價／雷達／台美股 watcher 的實作陷阱、改 `WEIGHTS` 必做三件事 → 下方〈已知陷阱〉索引對應的 rules 檔。

---

## 已知陷阱（跨功能通用）

> **序號會隨增刪漂移——本檔外部一律引「標題」不引序號。** 2026-08-10 清過一輪：治理文件 8 處
> 序號引用已全改標題（歷史 plan／AUDIT 刻意留原樣）。刪條目時**留占位不重編**。
> **2026-09-15 起多數條目逐字搬到 `.claude/rules/`**（Read 到對應檔自動載入）；下表是標題索引，編號不變。

| No. | 標題 | 正本（`.claude/` 底下） |
|---|---|---|
| — | service 層 fallback chain（讀 code 看不出順序，改動前必看） | rules/service-data.md |
| — | 最低價綜合評估（四季論底部） | rules/radar-core.md |
| — | 相對高/低點雷達（逃頂＋抄底） | rules/radar-core.md |
| — | 改動守則（違反即 bug） | 本檔〈三大核心系統〉＋ rules/radar-core.md |
| — | 台股/美股版（watcher 股票分支） | rules/tw-us-watcher.md |
| 1 | `@st.fragment` 靜默失效（現價停止自動更新） | rules/streamlit-ui.md |
| 2 | `@st.cache_data(ttl=60)` + `run_every=60` 衝突 | rules/streamlit-ui.md |
| 3 | 分辨「fragment 沒跑」還是「連線問題」（公司網路是 SSL 攔截，非封鎖） | rules/streamlit-ui.md |
| 4 | service 層來源追蹤慣例 | rules/service-data.md（占位） |
| 5 | `reindex(method='nearest')` 早於資料起點填充定值 | rules/streamlit-ui.md |
| 6 | 資金費率即時備援鏈 | rules/service-data.md |
| 7 | 圖表欄位名與顯示標籤混淆 | rules/streamlit-ui.md |
| 8 | AHR999 冪律公式（舊版膨脹至 $177 萬） | rules/radar-core.md |
| 9 | Walk-Forward 雙重移位 | rules/radar-core.md |
| 10 | Walk-Forward 進場乖離硬編碼 1.5% 上限 | rules/radar-core.md |
| 11 | 派網 Bot API 不支援幣本位網格與馬丁格爾 | 本檔下方 |
| 12 | 新聞來源限制 | rules/news-gemini.md |
| 13 | 新聞中文化省 token 三層（`service/news_i18n.py`） | rules/news-gemini.md |
| 14 | Gemini 兩坑 | rules/news-gemini.md |
| 15 | requirements 勿替 numpy/pandas 加上限 | rules/requirements.md |
| 16 | 檔案頂層 import 會拖進重依賴 | rules/service-data.md |
| 17 | `_df_from_sqlite` 曾強制欄名全轉小寫 | rules/service-data.md |
| 18 | 分位型維度的「母體長度」是口徑的一部分 | rules/tw-us-watcher.md |
| 19 | Yahoo 台股「有價無量」幽靈列 | rules/tw-us-watcher.md |
| 20 | 「分位」與「量比」使用者一定會互相驗算 | rules/tw-us-watcher.md |
| 21 | 公開檔的「範例數字」曾是真數字（S-1 私有化做一半） | 本檔下方 |
| 22 | P4 重啟偵測曾掛在錯的觸發點上 | rules/radar-core.md |
| 23 | 測試自己會騙人的兩種形狀（2026-09-11 同日各踩一次） | rules/testing.md |

### 11. 派網 Bot API 不支援幣本位網格與馬丁格爾

`buOrderTypes` 只有 `futures_grid`／`spot_grid`／`smart_copy`；App 手動建的機器人不會出現在
API 回傳，帳戶餘額 API 也不含機器人內資產。→ **派網 API Key 對本專案無用，勿再嘗試。**

### 21. 公開檔的「範例數字」曾是真數字（S-1 私有化做一半）

`config_private.py.example` 進公開版控，其 docstring 的 JSON schema 範例在 2026-07-06
S-1 私有化時**直接抄了真實防守數字**（觸發價／釋出量／加保後強平價），一年多來公開可讀，
2026-08-21 才清掉。**S-1 的威脅模型是「repo 是 public」，不是「config.py 這個檔」**——
搬走真值卻在隔壁檔案的註解裡留副本＝沒搬。

**禁令**：`.example`、README、CLAUDE.md、測試檔、commit message **一律只寫顯假值**
（99999/88888/77777）。結構可以真，數字不准真。測試要真值就 `from config_private import`
＋缺檔 skip（見 `tests/test_defense_ladder.py`）。改任何公開檔前先問「這個數字是不是部位」。

---

## 受保護決策（改動需使用者裁定）

| 項目 | 規則 | 正本 |
|---|---|---|
| 防守通知數字 | 真實數字在 `config_private.py`（gitignored）或 Actions Secret `DEFENSE_CONFIG_JSON`，公開 `config.py` 只留載入邏輯（fail-loud）。**馬丁止盈重啟即整表作廢**（新最後加倉價＝新起始價×0.659，整表重算）——2026-09-07 起**無自動偵測**，防守推播固定帶一行「執行前必對帳重算」靜態警語。防守為**條件式**：每階執行前看 `final_low`/`ensemble_low`。**`ALERT_PRICE_LOW` 自 2026-08-21 起與第 1 階解耦**（獨立預警價，判準 `>=` 不再是 `==`）| vault「1a 1 BTC ROAD.md」「二、防守機制」；驗算見「1b 馬丁格爾數學稽核」；`_governance\歷程\20260706stress_btc三軌壓測.md` |
| 雙幣回測 | **舊曲線與據其做的結論全部作廢**（權利金曾在結算日才定價，全史 +1733%→−90%）。**雙幣加碼決策不可依據此回測模組** —— 實際用法是梯形建議＋偏保守權重。殘留已知偏差：σ 用 ATR/close proxy 高估 ~1.6×（方向已知、接受） | `calculate_ladder_strategy` docstring（2026-06-17 拍板） |
| 四季論引擎 | `SEASON_ENGINE` **現為 `"v2"`**（2026-09-03 使用者拍板由 `"v1"` 切換；十六象限＝市場軸補 `deep_bear` 一級＋防抖逃生門，見 README v3.50）。**切回 v1、改象限表或防抖參數都需使用者裁定**；回滾＝改回 `"v1"` 一個字（已實測）。回滾／重驗條件：某次熊底**下跌段**出現 v1/v2 分歧即回滾；新一輪熊底走完以準則 2a/2b 重驗。**不可回頭調參數讓驗收準則「看起來過」** | vault `Github\Cow\歷程\20260902findings_四季論v2象限擴充與回放.md`（切換依據）；`Github\Cow\歷程\20260706findings_四季論v2回放對照.md`（初版回放）；設計正本 `Github\Cow\season_v2_design.md`；觸發條件 `_governance\LEDGER-constants-liveness.md` |
