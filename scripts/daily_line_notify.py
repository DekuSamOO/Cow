"""
scripts/daily_line_notify.py
用於 GitHub Actions 的戰情室自動推播腳本。
同步更新：補齊分數計算、MA200對比、0.03%費率燈號及五段式建議邏輯。
v2: 整合原 JustVibe 四季日報內容（季節徽章 + 底部支撐三指標），統一單一推播。
"""

import os
import sys
import json
import urllib3
import requests
from core.http_client import safe_get, safe_post
from datetime import datetime, timezone, date

SEASON_BG_COLOR = {
    "spring": "#27AE60",
    "summer": "#E67E22",
    "autumn": "#E74C3C",
    "winter": "#2980B9",
}

SEASON_LIGHT_BG = {
    "spring": "#E8F5E9",
    "summer": "#FFF3E0",
    "autumn": "#FFEBEE",
    "winter": "#E3F2FD",
}

SEASON_DESC = {
    "spring": "減半後 0–12 個月，市場低調吸籌",
    "summer": "減半後 12–18 個月，主升浪爆發",
    "autumn": "減半後 18–36 個月，獲利了結回落",
    "winter": "減半後 36–48 個月，長期底部整理",
}

# 把舊深色主題用的螢光色映射為白底可讀的深色版
LIGHT_REMAP = {
    "#00ff88": "#27AE60",
    "#ff4b4b": "#E74C3C",
    "#ffeb3b": "#F39C12",
    "#aaaaaa": "#888888",
    "#ffffff": "#2C3E50",
    "#ffcc66": "#E67E22",
}


def _light(c: str) -> str:
    """把深色主題的色值映射成白底可讀的版本；其他色不動。"""
    return LIGHT_REMAP.get(c, c)


# ==============================================================================
# 環境設定：與 config.py 共用 SSL_VERIFY 旗標，避免重複推導邏輯
# ==============================================================================
from config import SSL_VERIFY
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from service.realtime import fetch_realtime_data
from service.market_data import fetch_market_data
from service.onchain import _fetch_funding_rate_history
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators, calculate_market_cycle_score
from core.season_forecast import forecast_price, STATS as _SEASON_STATS
from core.bottom_floors import compute_all_bottom_estimates
from service.bottom_metrics import get_latest_bottom_metrics, fetch_hashrate_history_ths


def _get_cycle_meta(score: int):
    if score >= 75: return "🔥 狂熱牛頂", "#ff4b4b", "風險極高，建議分批止盈。"
    elif score >= 40: return "🐂 牛市主升段", "#ff9800", "趨勢多頭排列，可持有並設移動止盈。"
    elif score >= 15: return "🌱 初牛復甦", "#8bc34a", "市場轉暖，分批建倉機會。"
    elif score >= -15: return "⚪ 中性過渡", "#9e9e9e", "多空均衡，觀望為主。"
    elif score >= -40: return "📉 轉折回調", "#7986cb", "趨勢轉弱，建議輕倉。"
    elif score >= -75: return "❄️ 熊市築底", "#42a5f5", "開始定投積累。"
    else: return "🟦 歷史極值底部", "#00bcd4", "All-In 信號！歷史罕見買入機會。"

def get_decision_data():
    summary = {
        "price": "N/A", "current_price": 0.0,
        "cycle_score": 0, "cycle_name": "N/A", "cycle_color": "#aaaaaa", "cycle_advice": "",
        "ma200_label": "N/A", "funding_text": "N/A", "funding_color": "#aaaaaa",
        "trend_text": "N/A", "trend_color": "#aaaaaa",
        "rsi_text": "N/A", "rsi_color": "#aaaaaa",
        "macd_text": "N/A", "macd_color": "#aaaaaa",
        "adx_text": "N/A", "adx_color": "#aaaaaa",
        "ema_dist_text": "N/A", "ema_dist_color": "#aaaaaa",
        "swing_advice": "N/A", "swing_advice_color": "#aaaaaa",
        "forecast_type": "bear_bottom", "target_low": 0, "target_median": 0, "target_high": 0,
        "label_low": "最深", "label_high": "最淺",
        "forecast_estimated_date": "N/A", "forecast_ath_ref": 0,
        # ── 四季區塊（自原 JustVibe 四季日報整合）──
        "season_emoji": "❓", "season_zh": "N/A", "season_color": "#888888", "season_desc": "",
        "halving_date_str": "N/A", "days_since_halving": 0, "cycle_progress_pct": 0,
        "cycle_ath": 0, "cycle_ath_date": "N/A", "from_high_pct": 0.0,
        # ── 底部支撐三指標 ──
        "floor_ma200w": None, "floor_power_law": None, "floor_miner_cost": None,
    }

    current_price = None
    try:
        # Coinbase 公開 API：GitHub Actions 伺服器在美國，Binance 會 451 Geo-block
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        response = safe_get(url, verify=SSL_VERIFY, timeout=10)
        response.raise_for_status()
        current_price = float(response.json()['data']['amount'])
        print(f"✅ 成功透過 Coinbase API 抓取最新 BTC 價格: {current_price}")

    except Exception as e:
        print(f"❌ 抓取 Coinbase 即時價格失敗，錯誤原因: {e}")

    try:
        btc_df, _ = fetch_market_data()
        funding_df = _fetch_funding_rate_history()

        latest_funding = 0.0
        if not funding_df.empty:
            latest_funding = funding_df['fundingRate'].iloc[-1]
            f_is_hot = latest_funding >= 0.03
            summary["funding_text"] = f"{'🔴' if f_is_hot else '🟢'} {latest_funding:.4f}%"
            summary["funding_color"] = "#ff4b4b" if f_is_hot else "#00ff88"

        if not btc_df.empty:
            # 補齊所有指標計算以對齊羅盤分數
            btc_df = calculate_technical_indicators(btc_df)
            btc_df = calculate_ahr999(btc_df)
            btc_df = calculate_bear_bottom_indicators(btc_df)

            curr = btc_df.iloc[-1].copy()

            # 若上方 Coinbase API 抓取失敗，則使用歷史 K 棒的最後一筆收盤價作為備用
            if current_price is None:
                current_price = float(curr['close'])

            curr['close'] = current_price
            summary["price"] = f"${current_price:,.0f}"
            summary["current_price"] = current_price
            curr['funding_rate'] = latest_funding

            # MA200 狀態標籤
            ma200 = curr.get('SMA_200', 0)
            ma_is_higher = ma200 > current_price
            summary["ma200_label"] = f"{'🔴' if ma_is_higher else '🟢'} ${ma200:,.0f} ({'>' if ma_is_higher else '<'} 現價)"

            # 預測區塊（同步抓出季節資訊填入四季徽章）
            f_res = forecast_price(current_price, btc_df)
            if f_res:
                summary.update({
                    "forecast_type": f_res["forecast_type"],
                    "target_low": f_res["target_low"], "target_median": f_res["target_median"], "target_high": f_res["target_high"],
                    "label_low": "最深" if "bear" in f_res["forecast_type"] else "保守",
                    "label_high": "最淺" if "bear" in f_res["forecast_type"] else "樂觀",
                    "forecast_ath_ref": f_res.get("ath_ref") or 0,
                })
                est = f_res.get("estimated_date")
                if est:
                    summary["forecast_estimated_date"] = est.strftime("%Y-%m")

                season_info  = f_res.get("season_info") or {}
                eff_season   = f_res.get("effective_season") or {}
                market_state = f_res.get("market_state") or {}

                season_key = eff_season.get("season") or season_info.get("season") or "spring"
                summary["season_emoji"] = eff_season.get("emoji") or season_info.get("emoji") or "❓"
                summary["season_zh"]    = eff_season.get("season_zh") or season_info.get("season_zh") or "N/A"
                summary["season_color"] = SEASON_BG_COLOR.get(season_key, "#888888")
                summary["season_desc"]  = SEASON_DESC.get(season_key, "")

                halving_dt = season_info.get("halving_date")
                if halving_dt:
                    summary["halving_date_str"]  = halving_dt.strftime("%Y-%m-%d")
                summary["days_since_halving"] = season_info.get("days_since", 0)
                summary["cycle_progress_pct"] = int(min(season_info.get("cycle_progress", 0.0), 1.0) * 100)

                cycle_ath = market_state.get("cycle_ath", 0) or 0
                cycle_ath_dt = market_state.get("cycle_ath_date")
                summary["cycle_ath"] = cycle_ath
                if cycle_ath_dt:
                    summary["cycle_ath_date"] = cycle_ath_dt.strftime("%Y-%m-%d")
                if cycle_ath > 0:
                    summary["from_high_pct"] = (current_price - cycle_ath) / cycle_ath * 100

            # ── 最低價綜合評估（單一真實來源 core/bottom_floors，dashboard 共用）──
            now_utc = datetime.now(timezone.utc)
            hr_hist = fetch_hashrate_history_ths()
            latest_hash = hr_hist[max(hr_hist)] if hr_hist else None
            bottom_eval = compute_all_bottom_estimates(
                current_price, df=btc_df, now=now_utc,
                hashrate_ths=latest_hash,
                onchain=get_latest_bottom_metrics(),
            )
            summary["bottom_eval"] = bottom_eval
            # 舊 floor 欄位由同源 bottom_eval 回填（向後相容，避免兩邊漂移）
            _by = {e["key"]: e["value"] for e in bottom_eval["estimates"]}
            summary["floor_ma200w"]    = _by.get("ma200w")
            summary["floor_power_law"] = _by.get("power_law")
            summary["floor_miner_cost"] = bottom_eval.get("miner_elec")

            # 分數計算
            score = calculate_market_cycle_score(curr)
            summary["cycle_score"] = score
            summary["cycle_name"], summary["cycle_color"], summary["cycle_advice"] = _get_cycle_meta(score)

            # 波段雷達與五段式建議邏輯
            sma50 = curr.get('SMA_50', 0)
            is_bull_trend = current_price > ma200 and sma50 > ma200
            summary["trend_text"] = "🟢 多頭排列" if is_bull_trend else "🔴 空頭/震盪"
            summary["trend_color"] = "#00ff88" if is_bull_trend else "#ff4b4b"

            rsi = curr.get('RSI_14', 0)
            summary["rsi_text"] = f"{'🟢' if rsi > 50 else '🔴'} ({rsi:.1f})"
            summary["rsi_color"] = "#00ff88" if rsi > 50 else "#ff4b4b"

            macd, macd_sig = curr.get('MACD', 0), curr.get('MACD_Signal', 0)
            summary["macd_text"] = "🟢 金叉" if macd > macd_sig else "🔴 死叉"
            summary["macd_color"] = "#00ff88" if macd > macd_sig else "#ff4b4b"

            adx = curr.get('ADX_14', 0)
            summary["adx_text"] = f"{'🟢' if adx > 20 else '🔴'} ({adx:.1f})"
            summary["adx_color"] = "#00ff88" if adx > 20 else "#ff4b4b"

            ema20 = curr.get('EMA_20', 0)
            ema_dist = (current_price - ema20) / ema20 * 100 if ema20 > 0 else 0
            summary["ema_dist_text"] = f"{'🟢' if 0 <= ema_dist <= 1.5 else '🔴'} ({ema_dist:.1f}%)"
            summary["ema_dist_color"] = "#00ff88" if 0 <= ema_dist <= 1.5 else "#ff4b4b"

            # 綜合建議判斷
            if is_bull_trend:
                if 0 <= ema_dist <= 1.5 and rsi > 50 and macd > macd_sig and adx > 20:
                    summary["swing_advice"] = "🚀 動能共振！絕佳進場買點"
                    summary["swing_advice_color"] = "#00ff88"
                elif ema_dist > 1.5:
                    summary["swing_advice"] = "📈 趨勢偏多，但乖離過大不宜追高"
                    summary["swing_advice_color"] = "#ffeb3b"
                else:
                    summary["swing_advice"] = "🟡 多頭排列，等待動能指標轉強"
                    summary["swing_advice_color"] = "#ffeb3b"
            else:
                if ema_dist < 0:
                    summary["swing_advice"] = "❄️ 跌破短期均線，建議觀望"
                    summary["swing_advice_color"] = "#ff4b4b"
                else:
                    summary["swing_advice"] = "⚪ 趨勢偏弱，空頭或震盪格局"
                    summary["swing_advice_color"] = "#aaaaaa"

            # ── 波段雷達（逃頂＋抄底）＋四季雷達頂錨（同源 core/relative_high + relative_low）──
            try:
                summary.update(_compute_radars(btc_df, curr, latest_funding, current_price))
            except Exception as _ee:
                print(f"[WARN] radar score: {_ee}")

    except Exception as e: print(f"Data error: {e}")
    return summary

def fetch_news_digest(limit: int = 10, top: int = 8) -> dict:
    """抓取新聞輿情摘要供推播：整體情緒燈號 + 前幾則重大新聞中文標題。
    任何失敗都回安全預設（推播照常，只是省略新聞區塊）。
    在 Actions（無 streamlit runtime）用 .__wrapped__ 繞過 @st.cache_data。
    """
    out = {"news_mood": None, "news_items": []}
    try:
        from service.news import fetch_crypto_news, summarize_sentiment, SENTIMENT_EMOJI
        feed = fetch_crypto_news.__wrapped__(limit)
        shown = (feed.items or [])[:top]   # 只取要推播的前 top 則
        if not shown:
            return out

        sent = summarize_sentiment(shown)  # 輿情計數與實際顯示則數一致
        if sent.mood:
            out["news_mood"] = f"{sent.mood}（多{sent.bull}／空{sent.bear}／中{sent.neutral}）"

        for it in shown:
            out["news_items"].append({
                "emoji": SENTIMENT_EMOJI.get(it.sentiment or "", "•"),
                "title": it.title_zh or it.title,
            })
    except Exception as e:
        print(f"[WARN] news digest: {e}")
    return out


def _compute_radars(btc_df, curr, latest_funding, price) -> dict:
    """
    波段雷達（逃頂＋抄底）＋四季雷達頂錨/牛頂熊底分，一次算齊（共用同一批外部資料）。
    與 dashboard 波段/四季雷達、BTC_WATCH 同源 core/relative_high + relative_low。
    GH Actions 無 live Binance OI（geo-block）→ oi_stats=None；其餘維度盡量補齊。
    macro 同時帶逃頂（hot/strong）與抄底（cool/weak）兩套欄位。
    """
    from core.relative_high import compute_relative_high
    from core.relative_low import compute_relative_low
    from core.trend_direction import compute_trend_direction
    from service.bottom_metrics import get_latest_bottom_metrics
    from service.market_snapshot import get_btcd_trend
    from service.etf_flow import get_etf_flow_summary
    from service.macro_data import (get_next_macro_event, fetch_us_cpi_yoy,
                                    fetch_us_pce_yoy, fetch_nfp, fetch_unrate)

    fng = None
    try:
        r = safe_get("https://api.alternative.me/fng/", timeout=8, verify=SSL_VERIFY)
        if r.status_code == 200:
            fng = float(r.json()["data"][0]["value"])
    except Exception:
        pass

    sopr = btcd = etf = macro = None
    try: sopr = get_latest_bottom_metrics().get("sopr")
    except Exception: pass
    try: btcd = get_btcd_trend()
    except Exception: pass
    try: etf = get_etf_flow_summary()
    except Exception: pass
    try:
        cpi = fetch_us_cpi_yoy(); pce = fetch_us_pce_yoy()
        nfp = fetch_nfp(); unr = fetch_unrate(); nxt = get_next_macro_event()
        nfp_k = nfp.get("change_k") or 0; unr_pp = unr.get("change_pp") or 0
        ct = cpi.get("trend") or ""; pt = pce.get("trend") or ""
        macro = {
            "cpi_hot": "升溫" in ct, "pce_hot": "升溫" in pt,
            "jobs_strong": (nfp_k > 150) or (unr_pp < 0),
            "cpi_cool": "降溫" in ct, "pce_cool": "降溫" in pt,
            "jobs_weak": (nfp_k < 50) or (unr_pp > 0),
            "event_within_days": nxt.get("days"),
        }
    except Exception:
        pass

    common = dict(funding_8h=latest_funding, oi_stats=None, etf_summary=etf,
                  sopr=sopr, fng=fng, btc_d_trend=btcd, macro=macro)
    rh = compute_relative_high(price, curr, btc_df, **common)
    rl = compute_relative_low(price, curr, btc_df, **common)
    td = compute_trend_direction(curr, btc_df)
    ct_state = rh["cycle_top"]
    return {
        # 波段雷達 · 逃頂
        "escape_score": rh["escape_score"], "escape_level": rh["escape_level"],
        "escape_color": rh["escape_color"], "escape_action": rh["escape_action"],
        "escape_signals": rh["escape_signals"],
        # 波段雷達 · 抄底
        "low_score": rl["low_score"], "low_level": rl["low_level"],
        "low_color": rl["low_color"], "low_action": rl["low_action"],
        "low_signals": rl["low_signals"],
        "trend_score": td["trend_score"], "trend_level": td["trend_level"],
        "trend_color": td["trend_color"], "trend_action": td["trend_action"],
        "trend_signals": td["trend_signals"],
        # 四季雷達 · 週期頂錨 + 牛頂/熊底分（cycle_top 只取可序列化的純量欄位）
        "top_estimates": rh["top_estimates"],
        "cycle_top": {"bull_total": ct_state.get("bull_total", 0),
                      "bear_total": ct_state.get("bear_total", 0),
                      "effective_season": ct_state.get("effective_season"),
                      "is_autumn": ct_state.get("is_autumn", False)},
    }


def send_line_message(flex_payload):
    from service.notification.core import _send_line_message
    _send_line_message([flex_payload])


# 獨立 state 檔（與 price_alert.py 的 alert_state.json 分開，避免兩 workflow 互相覆蓋）
_ESCAPE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "escape_alert_state.json")


def maybe_send_escape_alert(data: dict) -> None:
    """逃頂評分 ≥ 門檻時，額外推一則 LINE 文字警報（每曆日最多一次，防洗版）。"""
    from config import ESCAPE_ALERT_THRESHOLD
    score = data.get("escape_score", 0) or 0
    if score < ESCAPE_ALERT_THRESHOLD:
        print(f"✓ 逃頂評分 {score} < {ESCAPE_ALERT_THRESHOLD}，不觸發逃頂警報。")
        return

    state = {}
    if os.path.exists(_ESCAPE_STATE_FILE):
        try:
            with open(_ESCAPE_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass
    today = str(date.today())
    if state.get("last_escape_date") == today:
        print(f"ℹ️ 逃頂評分 {score} ≥ {ESCAPE_ALERT_THRESHOLD}，但今日已推播逃頂警報，略過。")
        return

    try:
        from service.notification.builders import build_escape_alert_flex
        from service.notification.core import _send_line_message
        flex = build_escape_alert_flex(data)
        if flex is None:
            print("⚠️ 逃頂警報 Flex 無法建構（缺 escape_signals），略過。")
            return
        _send_line_message([flex])
        state["last_escape_date"] = today
        with open(_ESCAPE_STATE_FILE, "w") as f:
            json.dump(state, f)
        print(f"🚨 已發送逃頂警報 Flex（評分 {score}）。")
    except Exception as e:
        print(f"❌ 逃頂警報發送失敗: {e}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    from service.notification.builders import build_flex_message
    load_dotenv()
    data = get_decision_data()
    data.update(fetch_news_digest())
    send_line_message(build_flex_message(data))
    # 逃頂警報：抖進每日推播，超門檻才額外推一則（每日去重）
    maybe_send_escape_alert(data)
