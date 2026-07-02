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
