
"""
config.py — 比特幣投資戰情室 超參數集中管理

將原本散落各模組的硬編碼 (hardcode) 常數集中至此，
方便統一調整、測試與版本管控。

SSL 動態驗證機制說明
─────────────────────────────────────────────────────────
本地端開發（公司網路）：IS_STREAMLIT_CLOUD=false（預設）→ SSL_VERIFY=False
雲端部署（Streamlit Cloud）：在 Streamlit Cloud Secrets 或環境變數中設定
  IS_STREAMLIT_CLOUD=true → SSL_VERIFY=True

在 Streamlit Cloud 設定方式（任選其一）：
  1. Streamlit Cloud → App settings → Secrets：
     IS_STREAMLIT_CLOUD = "true"
  2. .env 檔案（本地測試雲端模式）：
     IS_STREAMLIT_CLOUD=true
─────────────────────────────────────────────────────────
"""
import os

# ==============================================================================
# 環境偵測 & SSL 驗證控制
# ==============================================================================
# 若環境變數 IS_STREAMLIT_CLOUD=true，代表當前為雲端部署，啟用 SSL 驗證
# 若未設定或為其他值，視為本地開發環境，關閉 SSL 驗證以繞過企業 Proxy
IS_STREAMLIT_CLOUD: bool = os.getenv("IS_STREAMLIT_CLOUD", "false").lower() == "true"

# 全域 SSL 驗證旗標：給 safe_get(..., verify=SSL_VERIFY) 使用
SSL_VERIFY: bool = IS_STREAMLIT_CLOUD

# ==============================================================================
# 新聞中文化（Gemini）總開關 — 成本剎車
# ==============================================================================
# 設 NEWS_I18N_ENABLED=false 可完全停用 Gemini 翻譯（新聞改顯示英文原文，0 API 呼叫）。
# 預設 true。即使開啟，仍有「持久化快取（翻過不重翻）＋ 4h 記憶體快取＋休眠 0 呼叫」三層省 token。
NEWS_I18N_ENABLED: bool = os.getenv("NEWS_I18N_ENABLED", "true").lower() == "true"

# ==============================================================================
# 資金管理 (Money Management)
# ==============================================================================
# 回測與倉位計算器的預設初始本金（USDT）
DEFAULT_INITIAL_CAPITAL: float = 10_000.0

# 每筆交易的預設風險百分比（佔總資金的 %）
DEFAULT_RISK_PER_TRADE: float = 1.0  # 1%

# ==============================================================================
# 技術指標參數 (Technical Indicator Parameters)
# ==============================================================================
# 長期趨勢均線（牛市濾網：收盤價需高於此線）
SMA_LONG_PERIOD: int = 200   # SMA 200

# 短期動能均線（進出場依據：Antigravity v4 核心）
EMA_SHORT_PERIOD: int = 20   # EMA 20

# 中期均線（雙幣理財牛熊濾網）
SMA_MID_PERIOD: int = 50     # SMA 50

# RSI 計算周期
RSI_PERIOD: int = 14

# ==============================================================================
# 波段策略進出場條件 (Swing Strategy Entry/Exit)
# ==============================================================================
# 進場甜蜜點：收盤價高於 EMA20 的最小乖離百分比（防止在 EMA20 以下進場）
ENTRY_DIST_MIN_PCT: float = 0.0   # 0%（貼近 EMA20）

# 趨勢過濾：RSI 需高於此值才視為多頭動能
EXIT_RSI_MIN: int = 50

# ==============================================================================
# Walk-Forward 回測策略 (Walk-Forward Backtest)
# ==============================================================================
# 出場模式：'simple' 或 'multi'
WALK_FORWARD_EXIT_MODES = {
    "simple": "簡化模式：只看防守線跌破",
    "multi": "進階模式：6層多重機制（Climax/ATR/Chandelier/EMA等）",
}
DEFAULT_WALK_FORWARD_EXIT_MODE: str = "simple"

# ==============================================================================
# 交易成本 (Transaction Costs)
# ==============================================================================
# 交易手續費率（Taker Fee）：幣安現貨預設 0.1%，VIP 用戶可調低
DEFAULT_FEE_RATE: float = 0.001   # 0.1%

# 滑點估算：下單時因市場深度不足導致的成交價偏差，保守估計 0.1%
DEFAULT_SLIPPAGE_RATE: float = 0.001  # 0.1%

# ==============================================================================
# 雙幣理財策略 (Dual Investment Strategy)
# ==============================================================================
# 結算後空窗期（天）：模擬真實操作中，結算後需觀察市場 1 天再重新開單
DUAL_INVEST_COOLDOWN_DAYS: int = 1

# ==============================================================================
# 通知門檻 (Notification Thresholds)
# ==============================================================================
# 雙幣 APY 推播門檻：年化報酬率超過此值才觸發 LINE/Telegram 通知（%）
DEFAULT_APY_THRESHOLD: float = 20.0  # 20%

# ── S-1 覆蓋層（2026-07-06）：敏感防守數字（觸發價/釋出量/強平價/資金計畫）改私有來源載入 ──
# 公開版本檔只留結構與載入邏輯，真實數字不進版控。
# 本地：複製 config_private.py.example → config_private.py（已 .gitignore）填入真值。
# GitHub Actions：Repository Secret `DEFENSE_CONFIG_JSON`（JSON blob，schema 見 .example 檔）。
# fail-loud（憲法第 3 條）：兩者皆缺，於「首次實際使用」時 raise，絕不 fallback 假數字/空表。
# 用 module __getattr__（PEP 562）延遲載入，故 `import config` 存取其他常數（如 SSL_VERIFY）
# 不受影響——只有真正讀取 ALERT_PRICE_LOW/DEFENSE_LADDER/DEFENSE_DECISION_CARD 才觸發載入
# （等同「用到防守數字才要求私有來源存在」，未設定時該功能停止，其餘功能不受拖累）。
# 數字正本：vault「1b 1 BTC ROAD.md」§4.2 防守推移表；驗算：vault「1b 馬丁格爾數學稽核.md」。
# ⚠️ 馬丁重啟即作廢／防守為條件式／決策卡設計說明見 config_private.py.example 與 vault 正本，
#   本檔不再重複列出（數字已不在此處，說明留在私有檔與 vault）。
_DEFENSE_ATTRS = frozenset(
    ("ALERT_PRICE_LOW", "DEFENSE_LADDER", "DEFENSE_DECISION_CARD", "MART_TP_BASELINE")
)
_defense_cache = None


def _load_defense_config():
    global _defense_cache
    if _defense_cache is not None:
        return _defense_cache
    json_blob = os.getenv("DEFENSE_CONFIG_JSON")
    if json_blob:
        import json as _json
        data = _json.loads(json_blob)
        _defense_cache = (
            float(data["alert_price_low"]),
            tuple(tuple(row) for row in data["defense_ladder"]),
            tuple(data["defense_decision_card"]),
            # P4（2026-07-13）：馬丁止盈重啟偵測基線——「可選」鍵，缺值=偵測停用
            # 回退舊靜態警語（非防守數字本體，不觸發 fail-loud）。
            data.get("mart_tp_baseline"),
        )
        return _defense_cache
    try:
        import config_private as _cp
        _alp, _dl, _ddc = (
            _cp.ALERT_PRICE_LOW, _cp.DEFENSE_LADDER, _cp.DEFENSE_DECISION_CARD,
        )
        _mtb = getattr(_cp, "MART_TP_BASELINE", None)
    except ImportError as e:
        raise RuntimeError(
            "敏感防守數字缺失（S-1 覆蓋層，憲法第 3 條 fail-loud，拒絕假數字/空表）：\n"
            "  本地：複製 config_private.py.example 為 config_private.py 並填入真值\n"
            "  GitHub Actions：設定 Repository Secret DEFENSE_CONFIG_JSON\n"
            "防守推播已停止，直到私有來源就緒。"
        ) from e
    _defense_cache = (_alp, _dl, _ddc, _mtb)
    return _defense_cache


def __getattr__(name):
    if name in _DEFENSE_ATTRS:
        alp, dl, ddc, mtb = _load_defense_config()
        return {"ALERT_PRICE_LOW": alp, "DEFENSE_LADDER": dl,
                "DEFENSE_DECISION_CARD": ddc, "MART_TP_BASELINE": mtb}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 遲滯（hysteresis）：警報觸發後解除武裝，回升超過門檻＋此值才重新武裝，
# 防止價格在門檻附近震盪時隔日反覆推播（單次跌破只提醒一次）。
ALERT_PRICE_REARM_GAP: float = 500.0

# ── U5-①（2026-07-14）：防守警報重複策略（獨立通道的 LINE 版）──
# 同一支 LINE bot 無法分聲音，改以「重複策略」與日常推播區隔（U5 原文：聲音/重複策略
# 不同）：觸發時三連響 burst＋決策窗內按里程碑重推＋24h 屆滿收尾（接 U5-② 預設不防守）。
# 若日後建立防守專用 LINE channel，只需設 DEFENSE_LINE_CHANNEL_ACCESS_TOKEN／
# DEFENSE_LINE_USER_ID（env/secret），零代碼改動即分流（見 service/notification/core.py）。
DEFENSE_BURST_COUNT: int = 3            # 觸發時連響次數（含主訊息；獨立 push 呼叫才會逐響）
DEFENSE_DECISION_WINDOW_H: int = 24     # 決策窗小時數（U5-②：屆滿未行動＝預設不防守）
DEFENSE_REMINDER_HOURS: tuple = (1, 2, 3, 6, 12, 18, 24)  # 窗內重推里程碑（事件起算小時）

# ==============================================================================
# 相對高點（逃頂）LINE 警報門檻
# ==============================================================================
# 逃頂綜合評分 ≥ 此值才推播（抖進每日推播，每日最多一次）。
# 門檻 60（escape_top_meta「明確過熱」）為保守設計：歷史可回測維度（資金費率/技術/情緒）
# 在頂部最高僅 ~24-43 分，要達 60 必須 OI/ETF/背離等強訊號同時觸發＝多重確認頂部，
# 誤報極低、不洗版。回測顯示單看可回測維度無法分辨頂部（頂/非頂分數重疊），故此門檻
# 非統計最佳化，而是「需多維共振才觸發」；待 OI 快照累積數月後可重校。
ESCAPE_ALERT_THRESHOLD: int = 60

# 逃頂警報分級（降冪）：(下限分數, 等級名)。顏色與標題映射見 notification/builders.py。
ESCAPE_ALERT_TIERS: tuple = ((85, "危急"), (75, "警報"), (60, "預警"))

# 跨日再推條件：連續多日超門檻時，分數較上次推播 ≥ 此差值（或升級）才再推，避免每天重複。
ESCAPE_ALERT_REPUSH_DELTA: int = 5

# ==============================================================================
# 底部模型演算法參數（單一可調來源；core/bottom_floors、core/miner_cost 讀此處）
# 注：四季論各輪 peak/bottom mult 為歷史實測值（非可調參數），留在 core/season_forecast。
# ==============================================================================
# 各底部算法可靠度權重（滿分 100；綜合歷史抓底命中度 + 資料品質 + 理論紮實度 + 樣本數）。
# 用於 ensemble 加權中位數；miner_allin 為警示線（註定被跌破）不納入 ensemble。
BOTTOM_RELIABILITY: dict = {
    "realized":      82,   # Realized Price：全網成本基礎，熊底貼著它
    "ma200w":        80,   # 200 週均線：四輪一致、零假設
    "balanced":      78,   # Balanced Price：歷史大底精準錨
    "miner_elec":    75,   # 礦工電費硬地板：三輪從未跌破
    "miner_implied": 68,   # 電費 × MINER_BOTTOM_MULT 實證延伸
    "power_law":     66,   # 冪律下界：長期公允下緣
    "cvdd":          64,   # CVDD：歷史絕對底，近年偏保守
    "puell_floor":   64,   # Puell 底：礦工投降價，與電費硬地板互證、零新資料源
    "ahr999_floor":  62,   # AHR999 抄底頂：便宜區上界
    "mayer_floor":   60,   # Mayer 底：啟發式比例
    "season_bottom": 58,   # 四季論趨勢底：週期邏輯佳但 n=3 脆弱
    "miner_allin":   50,   # all-in 警示線（不納入 ensemble）
}
# 礦工電費歷史熊底倍數（2015/2018/2022 熊底/電費中位數 ≈ 1.10 的保守取值）
MINER_BOTTOM_MULT: float = 1.08
# Puell Multiple 底部門檻（日礦工發行美元 / 其365日均；歷史熊底 0.3~0.5，礦工投降）
PUELL_BOTTOM: float = 0.5
# Mayer Multiple 歷史底部區（價/2年線）
MAYER_BOTTOM_RATIO: float = 0.6
# AHR999 抄底區上界
AHR999_DCA_CEIL: float = 0.45
# 全網平均電價（USD/kWh）— 2026-06 對齊 Cambridge CBECI production-cost 標準值 0.05
# （舊值 0.055；改後 tests/bottom_floors_backtest.py section_a 三輪底/電費仍 >1，硬地板性質守）
MINER_ELECTRICITY_RATE: float = 0.05
# all-in / 純電費 加成（礦機折舊 + 場地 + 運維；業界估 1.5~2.0）
MINER_ALLIN_FACTOR: float = 1.6
# 全網平均礦機效率 anchor（ISO日期, J/TH）——業界粗估，分段線性插值；
# ⚠️ 礦工成本模型最大不確定來源，新機型世代交替時應更新末端 anchor
MINER_EFF_ANCHORS: tuple = (
    ("2013-01-01", 2000.0),
    ("2014-06-01",  800.0),
    ("2016-01-01",  250.0),
    ("2017-06-01",  130.0),
    ("2018-12-01",   95.0),
    ("2020-06-01",   60.0),
    ("2022-06-01",   40.0),
    ("2024-04-01",   28.0),
    ("2026-01-01",   24.0),
)

# ==============================================================================
# 四季論引擎版本（B1，2026-07-06 新增）
# ==============================================================================
# "v1"＝現行 _derive_real_season 散裝校正邏輯（預設，零行為變更）；
# "v2"＝十二象限二維狀態機（core/season_forecast.derive_effective_state，設計書
# Github\Cow\season_v2_design.md）——結構性根治 C-2（v1 在「時間入秋、市場未確認」
# 象限會誤出熊底預測）。**採用 v2 前必做回放對照**（design §4.2 三條驗收準則），
# 尚未做完前維持 "v1"，切換需使用者裁定（受保護設定，僅提議）。
SEASON_ENGINE: str = "v1"
