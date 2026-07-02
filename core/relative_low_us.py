"""
core/relative_low_us.py  ·  v0.1（2026-07-02 新建）
美股相對底部（抄底雷達）— 純函數、零網路請求。鏡像 `core/relative_high_us.py`，
同樣用純 OHLCV 通用軸組成（美股個股槓桿/法人/IV 無免費源）。

三維（0–100）：
  技術回穩 50（底背離 + RSI 超賣）        ← 複用 `core.relative_low_tw._score_technical_low`
  量價背離 30（量縮價增＝賣壓竭盡）       ← `core.relative_universal.score_volume_price_bottom`
  結構轉折 20（前低未破＝結構轉強）       ← `core.relative_universal.score_structure_bottom`

⚠️ 狀態：v0.1，全數規則式、尚未在美股資料上跑過任何回測，見 `core/relative_high_us.py` 同節說明
與 `core/relative_universal.py` 檔頭。權重配重理由同高點側鏡像。
"""
from typing import Dict, Tuple

from core.relative_low_tw import _score_technical_low
from core.relative_universal import (score_volume_price_bottom, score_structure_bottom, rescale_dim,
                                     low_meta_ladder)

WEIGHTS_LOW_US = {"technical": 50, "vol_price": 30, "structure": 20}
UNFITTED_DIMS_LOW_US = ("vol_price", "structure")


def compute_relative_low_us(row, df=None) -> Tuple[int, Dict[str, dict]]:
    """美股相對底部三維評分（0–100）。純 OHLCV，不需任何籌碼/估值資料。
    technical 沿用 relative_low_tw 的固定 max=25，須 rescale_dim 到本框架宣告的 50，
    否則實際可得分僅 25（跟 vol_price/structure 一樣要換算，不能漏，見 WEIGHTS_LOW_US）。"""
    signals = {
        "technical": rescale_dim(_score_technical_low(row, df), WEIGHTS_LOW_US["technical"]),
        "vol_price": rescale_dim(score_volume_price_bottom(df), WEIGHTS_LOW_US["vol_price"]),
        "structure": rescale_dim(score_structure_bottom(df), WEIGHTS_LOW_US["structure"]),
    }
    score = max(0, min(100, int(sum(s["score"] for s in signals.values()))))
    return score, signals


def relative_low_us_meta(score: int) -> Tuple[str, str, str]:
    return low_meta_ladder(score, "技術＋量價俱佳，分批進場（配合趨勢確認）")
