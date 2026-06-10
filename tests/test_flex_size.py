"""build_flex_message 的 payload 大小防線測試（不發送 LINE）。"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.notification.builders import (build_flex_message, _FLEX_SOFT_LIMIT_BYTES,
                                            _payload_size_bytes as _size)


def _minimal_summary():
    return {
        "price": "$100,000", "cycle_score": 0, "cycle_name": "N/A",
        "cycle_color": "#aaaaaa", "cycle_advice": "",
        "ma200_label": "N/A", "funding_text": "N/A", "funding_color": "#aaaaaa",
        "trend_text": "N/A", "trend_color": "#aaaaaa",
        "rsi_text": "N/A", "rsi_color": "#aaaaaa",
        "macd_text": "N/A", "macd_color": "#aaaaaa",
        "adx_text": "N/A", "adx_color": "#aaaaaa",
        "ema_dist_text": "N/A", "ema_dist_color": "#aaaaaa",
        "swing_advice": "N/A", "swing_advice_color": "#aaaaaa",
        "forecast_type": "bear_bottom", "target_low": 0, "target_median": 0, "target_high": 0,
        "label_low": "最深", "label_high": "最淺",
        # 季節區塊欄位（N/A = 區塊省略），對齊 scripts/daily_line_notify.get_decision_data 預設
        "season_emoji": "❓", "season_zh": "N/A", "season_color": "#888888", "season_desc": "",
        "halving_date_str": "N/A", "days_since_halving": 0, "cycle_progress_pct": 0,
        "cycle_ath": 0, "cycle_ath_date": "N/A", "from_high_pct": 0.0,
        "floor_ma200w": None, "floor_power_law": None, "floor_miner_cost": None,
    }


def test_normal_payload_under_limit():
    msg = build_flex_message(_minimal_summary())
    assert msg["type"] == "flex"
    assert _size(msg) <= _FLEX_SOFT_LIMIT_BYTES


def test_oversized_news_box_is_dropped():
    s = _minimal_summary()
    # 灌爆新聞區塊讓整體超過 40KB → 應自動移除新聞區塊
    s["news_mood"] = "測試"
    s["news_items"] = [{"emoji": "📰", "title": "超長標題" * 200} for _ in range(60)]
    msg = build_flex_message(s)
    body_texts = json.dumps(msg, ensure_ascii=False)
    assert "加密新聞輿情" not in body_texts
    assert _size(msg) <= _FLEX_SOFT_LIMIT_BYTES


def test_snapshot_stale_warning_rendered():
    s = _minimal_summary()
    s["snapshot_stale_days"] = 5
    msg = build_flex_message(s)
    assert "OI 快照已 5 天未更新" in json.dumps(msg, ensure_ascii=False)
    # 未過期（<=2 天）不顯示
    s["snapshot_stale_days"] = 1
    msg2 = build_flex_message(s)
    assert "未更新" not in json.dumps(msg2, ensure_ascii=False)
