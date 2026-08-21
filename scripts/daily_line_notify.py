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
from datetime import datetime, timezone, timedelta, date

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
from core.risk import compute_atr_risk
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

            # 週報用：近 7 日價格統計（週日傍晚場次 maybe_send_weekly_summary 取用）
            wk = btc_df.tail(7)
            if len(wk) >= 2:
                summary["week_high"] = float(wk["high"].max())
                summary["week_low"] = float(wk["low"].min())
                summary["week_change_pct"] = (current_price / float(wk["close"].iloc[0]) - 1) * 100
            summary["current_price"] = current_price
            curr['funding_rate'] = latest_funding

            # MA200 狀態標籤
            ma200 = curr.get('SMA_200', 0)
            ma_is_higher = ma200 > current_price
            summary["ma200_label"] = f"{'🔴' if ma_is_higher else '🟢'} ${ma200:,.0f} ({'>' if ma_is_higher else '<'} 現價)"

            # 預測區塊（同步抓出季節資訊填入四季徽章）
            f_res = forecast_price(current_price, btc_df)
            if f_res:
                # B1（2026-07-06）消費端盤點：SEASON_ENGINE="v2" 時 forecast_type 可能是
                # 'observe'（target_* 皆 None，設計刻意不出目標價）。_build_forecast_box
                # 對 target_low/median/high 做 :,.0f 格式化，None 會 TypeError——退回 0
                # 避免推播崩潰（目前 SEASON_ENGINE 預設 "v1" 永不觸發此分支，本守門為
                # 未來切換 v2 預先鋪路）。
                summary.update({
                    "forecast_type": f_res["forecast_type"],
                    "target_low": f_res["target_low"] if f_res["target_low"] is not None else 0,
                    "target_median": f_res["target_median"] if f_res["target_median"] is not None else 0,
                    "target_high": f_res["target_high"] if f_res["target_high"] is not None else 0,
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

            # ATR 風控框架（同源 core/risk，watcher 共用）：支撐用動態地板 final_low，
            # 算不出時 compute_atr_risk 內部退回近 60 日低點。
            summary["atr_risk"] = compute_atr_risk(
                btc_df, current_price, support=bottom_eval.get("final_low"))

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
    from core.action_ensemble import compute_composite_action
    from service.bottom_metrics import get_latest_bottom_metrics
    from service.market_snapshot import get_btcd_trend, get_snapshot_staleness_days
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

    sopr = btcd = etf = macro = mvrv_z = None
    try:
        _bm = get_latest_bottom_metrics()
        sopr = _bm.get("sopr")
        mvrv_z = _bm.get("mvrv_zscore")   # 2026-07 已驗證計入 onchain 子分，見 core/relative_high.py
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

    snapshot_stale = None
    try:
        snapshot_stale = get_snapshot_staleness_days()
    except Exception:
        pass

    common = dict(funding_8h=latest_funding, oi_stats=None, etf_summary=etf,
                  sopr=sopr, fng=fng, btc_d_trend=btcd, macro=macro, mvrv_z=mvrv_z)
    rh = compute_relative_high(price, curr, btc_df, **common)
    rl = compute_relative_low(price, curr, btc_df, **common)
    td = compute_trend_direction(curr, btc_df)
    ct_state = rh["cycle_top"]

    # 三軸合成行動建議（與 dashboard 同源 core/action_ensemble；傳 cycle 子分補強底部辨識）
    comp = compute_composite_action(td["trend_score"], rh["escape_score"], rl["low_score"],
                                    rl["low_signals"].get("cycle", {}).get("score"))
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
        # 三軸合成（comp 為 None 時欄位缺省，builders 自動隱藏該行）
        **({"composite_emoji": comp["emoji"], "composite_action": comp["action"],
            "composite_detail": comp["detail"], "composite_key": comp["action_key"],
            "composite_pos": comp["pos_label"], "composite_color": comp["color"]}
           if comp else {}),
        # 健康檢查：本機 OI 快照距今天數（>2 天時卡片顯示警告，揪出靜默失敗的排程）
        "snapshot_stale_days": snapshot_stale,
        # ETF 快取最新一筆距今天數（>4 天時卡片提示資料過舊，週末 1-3 天屬正常）
        "etf_stale_days": (etf or {}).get("stale_days"),
    }


def send_line_message(flex_payload):
    from service.notification.core import _send_line_message
    _send_line_message([flex_payload])


# 獨立 state 檔（與 price_alert.py 的 alert_state.json 分開，避免兩 workflow 互相覆蓋）
_ESCAPE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "escape_alert_state.json")


def _load_escape_state() -> dict:
    if os.path.exists(_ESCAPE_STATE_FILE):
        try:
            with open(_ESCAPE_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_escape_state(state: dict) -> None:
    with open(_ESCAPE_STATE_FILE, "w") as f:
        json.dump(state, f)


def attach_score_deltas(data: dict) -> None:
    """
    每日 Flex 的逃頂/抄底分數 Δ（vs 前一個推播日），寫入 data['escape_delta']/['low_delta']，
    並把今日分數存回 state（與逃頂警報共用 escape_alert_state.json，同一 artifact 持久化）。
    同日多次推播以首次寫入的當日分數為準更新，Δ 基準恆為「最近的前一日」。
    """
    state = _load_escape_state()
    hist = state.get("score_history") or {}
    today = str(date.today())
    prev_dates = sorted(d for d in hist if d < today)
    if prev_dates:
        prev = hist[prev_dates[-1]]
        if prev.get("escape") is not None and data.get("escape_score") is not None:
            data["escape_delta"] = int(data["escape_score"]) - int(prev["escape"])
        if prev.get("low") is not None and data.get("low_score") is not None:
            data["low_delta"] = int(data["low_score"]) - int(prev["low"])
    hist[today] = {"escape": data.get("escape_score"), "low": data.get("low_score")}
    state["score_history"] = {d: hist[d] for d in sorted(hist)[-8:]}  # 留近 8 日（Δ 用昨日、週報用整週）
    _save_escape_state(state)


def maybe_send_escape_alert(data: dict) -> None:
    """
    逃頂評分 ≥ 門檻時推 LINE 警報，分級 60 預警 / 75 警報 / 85 危急（config.ESCAPE_ALERT_TIERS）。
    防洗版三規則：
      1. 同曆日最多一次（原規則保留）。
      2. 跨日需「分數較上次推播 +ESCAPE_ALERT_REPUSH_DELTA」或「升級」才再推，
         連續多日同分不再重複轟炸。
      3. 低於門檻時解除武裝，下次再跨門檻視為新事件重新警報。
    """
    from config import ESCAPE_ALERT_THRESHOLD, ESCAPE_ALERT_REPUSH_DELTA
    from service.notification.builders import escape_alert_tier

    score = data.get("escape_score", 0) or 0
    state = _load_escape_state()

    if score < ESCAPE_ALERT_THRESHOLD:
        if state.get("last_escape_score") is not None:
            state.pop("last_escape_score", None)
            state.pop("last_escape_tier", None)
            _save_escape_state(state)
        print(f"✓ 逃頂評分 {score} < {ESCAPE_ALERT_THRESHOLD}，不觸發逃頂警報。")
        return

    today = str(date.today())
    tier_rank, tier_name = escape_alert_tier(score)
    last_score = state.get("last_escape_score")
    last_tier = state.get("last_escape_tier") or 0

    if state.get("last_escape_date") == today:
        print(f"ℹ️ 逃頂評分 {score} ≥ {ESCAPE_ALERT_THRESHOLD}，但今日已推播逃頂警報，略過。")
        return
    if (last_score is not None and tier_rank <= last_tier
            and score < last_score + ESCAPE_ALERT_REPUSH_DELTA):
        print(f"ℹ️ 逃頂評分 {score}（上次推播 {last_score}）未升級也未 +{ESCAPE_ALERT_REPUSH_DELTA}，"
              f"略過重複警報。")
        return

    try:
        from service.notification.builders import build_escape_alert_flex
        from service.notification.core import _send_line_message
        if last_score is not None:
            data["escape_prev_score"] = last_score
        flex = build_escape_alert_flex(data)
        if flex is None:
            print("⚠️ 逃頂警報 Flex 無法建構（缺 escape_signals），略過。")
            return
        _send_line_message([flex])
        state.update({"last_escape_date": today, "last_escape_score": score,
                      "last_escape_tier": tier_rank})
        _save_escape_state(state)
        print(f"🚨 已發送逃頂警報 Flex（{tier_name}，評分 {score}）。")
    except Exception as e:
        print(f"❌ 逃頂警報發送失敗: {e}")


def maybe_send_action_alert(data: dict, dry_run: bool = False) -> None:
    """
    三軸合成「行動」翻轉時推一則 LINE（action_key 與上次不同才推）。
    合成行動由日線驅動、每日至多變一次，3 次/日的排程足以當日捕捉翻轉。
    去重：同 action_key 不重推；首次觀測只記錄不推；狀態存共用 escape_alert_state.json。
    """
    key = data.get("composite_key")
    if not key:
        print("✓ 無合成行動（trend 缺），略過行動警報。")
        return
    state = _load_escape_state()
    prev_key = state.get("last_action_key")
    prev_label = state.get("last_action_label")

    if prev_key == key:
        print(f"✓ 合成行動未變（{key}），略過。")
        return

    if prev_key is None:
        print(f"✓ 首次記錄合成行動「{data.get('composite_action')}」，不推播。")
    else:
        from service.notification.builders import build_action_alert_flex
        flex = build_action_alert_flex(data, prev_label)
        if dry_run:
            print(f"[dry-run] 行動翻轉 {prev_label} → {data.get('composite_action')}；Flex altText="
                  f"{flex['altText']}（未發送）")
        else:
            try:
                send_line_message(flex)
                print(f"🔔 已發送行動翻轉警報：{prev_label} → {data.get('composite_action')}")
            except Exception as e:
                print(f"❌ 行動警報發送失敗: {e}")
                return  # 發送失敗不前進 state，下次可重試

    state["last_action_key"] = key
    state["last_action_label"] = data.get("composite_action")
    if not dry_run:
        _save_escape_state(state)


# 週報只在「傍晚 cron」觸發的 run 發。GitHub Actions 把觸發的 cron 放進 github.event.schedule，
# workflow 以 CRON_SCHEDULE 注入。一天僅一個 run 帶此值 → 即使排程延遲多個 run 都落在 ≥17 點，
# 也只有傍晚 cron 那個 run 會發 → 根治重複（不依賴跨 run artifact state，那個本就失效）。
_WEEKLY_CRON = "27 10 * * *"   # 台灣 18:27 場次（與 daily_line_notify.yml 第三個 cron 一致）


def maybe_send_mart_restart_alert(dry_run: bool = False) -> None:
    """P4 觸發點缺口修補（2026-08-21）：馬丁止盈重啟偵測改由「每日推播」驅動。

    原設計只在 notify_defense_line（價格跌破 config.ALERT_PRICE_LOW）時才呼叫
    detect_mart_restart——等於「要用防守階梯的那一刻，才發現階梯早就壞了」。
    實際後果：2026-07-13 的對帳基線在 8/19–8/21 上漲後失效（馬1 由第14輪跑到第20輪、
    馬2 由第12輪跑到第21輪，各重啟 6~9 次），偵測器一個多月未出聲，最後靠人工對帳發現。
    價格從沒跌破警報價，偵測邏輯就從沒被執行過。

    改為每日檢查後，任一馬丁止盈重啟 24 小時內即告警，不必等到防守事件。

    去重：key =（對帳基線日, 已重啟馬丁名單），同 key 只推一次；
    人工更新 MART_TP_BASELINE 後基線日改變 → key 改變 → 可再次告警。
    """
    from service.notification.facade import detect_mart_restart
    import config as _config

    baseline = getattr(_config, "MART_TP_BASELINE", None)
    if not baseline:
        # 下方 baseline["date"] 要用到本物件，故不倚賴 detect_mart_restart 內建的
        # config 回退（基線缺席時那條路徑會 AttributeError 拖垮整趟每日推播）。
        print("✓ 馬丁重啟偵測不可用（基線未設定），略過。")
        return
    info = detect_mart_restart(baseline)
    if info is None:
        print("✓ 馬丁重啟偵測不可用（行情取數失敗），略過。")
        return

    stale = [m for m in info if m["restarted"]]
    if not stale:
        print(f"✓ 馬丁重啟偵測：基線 {baseline['date']} 後高點 "
              f"${info[0]['max_high']:,.0f} 未達推斷止盈，防守階梯仍有效。")
        return

    key = baseline["date"] + "|" + ",".join(sorted(m["name"] for m in stale))
    state = _load_escape_state()
    if state.get("last_mart_restart_key") == key:
        print(f"✓ 馬丁重啟已告警過（{key}），略過。")
        return

    lines = ["🔁 防守階梯失效警告（P4 每日偵測）", "━━━━━━━━━━━━━━━━",
             f"對帳基線：{baseline['date']}"]
    for m in stale:
        lines.append(f"⚠ {m['name']} 推斷已止盈重啟：基線後高點 ${m['max_high']:,.0f}"
                     f" ≥ 推斷止盈 ${m['tp']:,.0f}")
        lines.append(f"　→ 第{m['rung']}階觸發價／釋出量作廢")
    lines += ["━━━━━━━━━━━━━━━━",
              "➡ 開派網 App →兩台馬丁「掛單詳情」，取本輪起始價，",
              "　依「最後加倉價 = 本輪起始價 × 0.92^5」重算 DEFENSE_LADDER，",
              "　同步 config_private.py／DEFENSE_CONFIG_JSON secret／vault「1b 1 BTC ROAD」§4.2。"]
    text = "\n".join(lines)

    if dry_run:
        print(text)
        return
    try:
        send_line_message({"type": "text", "text": text})
        state["last_mart_restart_key"] = key
        _save_escape_state(state)
        print("🔁 已發送馬丁重啟警告。")
    except Exception as e:
        print(f"❌ 馬丁重啟警告發送失敗: {e}")


def maybe_send_weekly_summary(data: dict, now=None) -> None:
    """
    週日「傍晚 cron 場次」加推一則週報，每週一次。
    內容：本週價格區間/漲跌、逃頂/抄底分數週高低（score_history 近 8 日）、趨勢與今日行動。
    """
    tw_now = now or datetime.now(timezone(timedelta(hours=8)))
    if tw_now.weekday() != 6:
        return
    # 限週日「傍晚」場次：時段閘門對本地/手動執行（CRON_SCHEDULE 空）也生效，
    # 避免週日早上跑就誤發（下方 cron 閘門只擋排程的非傍晚 run，擋不住本地）。
    if tw_now.hour < 17:
        return
    # 排程觸發：限傍晚 cron（免多個延遲 run 重複發）；手動 dispatch / 本地執行（CRON_SCHEDULE 空）放行
    cron = os.getenv("CRON_SCHEDULE", "")
    if cron and cron != _WEEKLY_CRON:
        print(f"ℹ️ 非傍晚 cron（{cron}），略過週報。")
        return
    state = _load_escape_state()
    today = tw_now.strftime("%Y-%m-%d")
    if state.get("last_weekly_date") == today:
        print("ℹ️ 本週週報已推播，略過。")
        return

    hist_vals = [v for _, v in sorted((state.get("score_history") or {}).items())]
    esc = [v["escape"] for v in hist_vals if v.get("escape") is not None]
    low = [v["low"] for v in hist_vals if v.get("low") is not None]

    # 優先 Flex 週報；資料不足回 None、或發送失敗 → 退回文字版（保留既有純文字格式）
    from service.notification.builders import build_weekly_flex
    flex = build_weekly_flex(data, esc, low, today)
    sent = False
    if flex is not None:
        try:
            send_line_message(flex)
            sent = True
            print("📒 已發送週報（Flex）。")
        except Exception as e:
            print(f"⚠️ 週報 Flex 發送失敗，改用文字版：{e}")

    if not sent:
        lines = [f"📒 BTC 週報（{today}）", "━━━━━━━━━━━━━━━━"]
        if data.get("week_change_pct") is not None:
            arrow = "📈" if data["week_change_pct"] >= 0 else "📉"
            lines.append(f"{arrow} 本週 {data['week_change_pct']:+.1f}%　現價 {data.get('price', '—')}")
            lines.append(f"週高 ${data['week_high']:,.0f}｜週低 ${data['week_low']:,.0f}")
        if esc:
            lines.append(f"🚨 逃頂分：週高 {max(esc):.0f}／週低 {min(esc):.0f}（現 {esc[-1]:.0f}，n={len(esc)}日）")
        if low:
            lines.append(f"🟢 抄底分：週高 {max(low):.0f}／週低 {min(low):.0f}（現 {low[-1]:.0f}）")
        if data.get("trend_level"):
            lines.append(f"🧭 趨勢：{data['trend_level']}")
        if data.get("composite_action"):
            lines.append(f"🎯 行動：{data['composite_action']}｜{data.get('composite_pos', '')}")
        if len(lines) <= 2:
            print("ℹ️ 週報資料不足，略過。")
            return
        try:
            send_line_message({"type": "text", "text": "\n".join(lines)})
            sent = True
            print("📒 已發送週報（文字）。")
        except Exception as e:
            print(f"❌ 週報發送失敗: {e}")
            return

    state["last_weekly_date"] = today
    _save_escape_state(state)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from service.notification.builders import build_flex_message
    load_dotenv()
    data = get_decision_data()
    data.update(fetch_news_digest())
    attach_score_deltas(data)  # 逃頂/抄底分數 vs 昨日 Δ，顯示於波段雷達區塊
    send_line_message(build_flex_message(data))
    # 逃頂警報：抖進每日推播，超門檻才額外推一則（分級 + 去重 + 遲滯）
    maybe_send_escape_alert(data)
    # 行動翻轉警報：三軸合成行動 key 變動才推一則（去重 + 首次只記錄）
    maybe_send_action_alert(data)
    # 馬丁止盈重啟：每日檢查對帳基線是否失效（P4 觸發點缺口修補，2026-08-21）
    maybe_send_mart_restart_alert()
    # 週報：週日傍晚場次加推一則（每週一次）
    maybe_send_weekly_summary(data)
