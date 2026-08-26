"""
core/relative_high_us.py  ·  v0.1（2026-07-02 新建）
美股相對高點（逃頂雷達）— 純函數、零網路請求。美股個股槓桿/法人/IV 無免費源
（見 watcher.py 既有註解），無法比照台股走籌碼四維；改以**純 OHLCV 通用軸**組成：

三維（0–100）：
  技術衰竭 50（頂背離 + RSI 超買）        ← 複用 `core.relative_high_tw._score_technical_high`
                                          （市場無關，已在加密/台股驗證的既有邏輯，非重造）
  量價背離 30（量增價縮＝出貨）           ← `core.relative_universal.score_volume_price_top`
  結構轉折 20（前高未過＝結構轉弱）       ← `core.relative_universal.score_structure_top`

⚠️ 狀態：v0.1，**全數規則式、尚未在美股資料上跑過任何回測**（技術維度在加密/台股驗證過，
但套用到美股本身仍是跨市場外插；量價/結構維度全部門派尚無回測，見 `core/relative_universal.py`）。
權重（50/30/20）為專家經驗值配重，非統計擬合結果——技術維度給多數權重是因為它是三者中唯一有
「別的市場」實證支持的模式，量價/結構完全未經驗證給較低權重。待累積美股 swing 資料後，
比照台股 v0.1→v0.4 的方法論用實測 AUC 重新配重。

════════════════════════════════════════════════════════════════════════════
⛔ 2026-08-26 端到端重驗：**確認無訊號，維持撤下，勿再撿起來用**
════════════════════════════════════════════════════════════════════════════
2026-07-04（C1）曾以「50 檔三維全近雜訊 AUC~0.5」把面板撤下。2026-08-26 用
**波動標準化**事件門檻（k_top=1.30 x 該標的當下 60 日 σ，正本
`Github\\Cow\\雷達評估標準.md`）在 SPY/QQQ/AAPL/NVDA/MSFT/AMZN/GOOGL/META 八檔重驗，
**兩種量法都是零訊號**（`tests/radar_decision_bench.py --asset us`）：

  計時量法  跨標的 AUC 中位 **0.500**（單檔全距 0.364~0.552，n=9~18 事件，幾乎全是雜訊）
  體制量法  分數 vs 其後 120 日報酬，跨標的 Spearman r 中位 **+0.011（期望負，方向還反了）**，
            方向正確僅 4/8 ＝ 擲硬幣

抄底側（`relative_low_us`）同批重驗：AUC 中位 0.521、體制 r 中位 +0.035（6/8）——同樣無訊號。

**重啟條件**（三條全滿足才重新評估，缺一不可）：
  1. 新增至少一個在**波動標準化標記**下單維 AUC >= 0.55 的維度（不是重配現有三維的權重）
  2. train/holdout 兩段都過，且 holdout 只驗一次
  3. 端到端 lift 在某個門檻 >= 1.5x（對照：台股逃頂 65 分是 1.93x）
**在此之前不要調權重、不要重新啟用面板。** 現有三維的配重是專家值，調它等於在沒有訊號的
刻度上重新分配權重——2026-08-25 加密側稽核已把這類動作歸為「治標不治本」。
"""
from typing import Dict, Tuple

from core.relative_high_tw import _score_technical_high
from core.relative_universal import (score_volume_price_top, score_structure_top, rescale_dim,
                                     high_meta_ladder)

WEIGHTS_HIGH_US = {"technical": 50, "vol_price": 30, "structure": 20}
# 全數未回測（規則式，非「回測後發現弱」），比照台股 UNFITTED_DIMS_* 的狀態分類。
UNFITTED_DIMS_HIGH_US = ("vol_price", "structure")


def compute_relative_high_us(row, df=None) -> Tuple[int, Dict[str, dict]]:
    """美股相對高點三維評分（0–100）。純 OHLCV，不需任何籌碼/估值資料。
    technical 沿用 relative_high_tw 的固定 max=30，須 rescale_dim 到本框架宣告的 50，
    否則實際可得分僅 30（跟 vol_price/structure 一樣要換算，不能漏，見 WEIGHTS_HIGH_US）。"""
    signals = {
        "technical": rescale_dim(_score_technical_high(row, df), WEIGHTS_HIGH_US["technical"]),
        "vol_price": rescale_dim(score_volume_price_top(df), WEIGHTS_HIGH_US["vol_price"]),
        "structure": rescale_dim(score_structure_top(df), WEIGHTS_HIGH_US["structure"]),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_high_us_meta(score: int) -> Tuple[str, str, str]:
    return high_meta_ladder(score, "技術＋量價俱過熱，分批止盈/減碼")
