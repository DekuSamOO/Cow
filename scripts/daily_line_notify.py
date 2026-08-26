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
            # 門檻收回 core 單一來源（原硬編 0.03；見 core.relative_high.FUNDING_HOT_8H）
            from core.relative_high import FUNDING_HOT_8H
            f_is_hot = latest_funding >= FUNDING_HOT_8H
            summary["funding_text"] = f"{'🔴' if f_is_hot else '🟢'} {latest_funding:.4f}%"
            summary["funding_color"] = "#ff4b4b" if f_is_hot else "#00ff88"

        if not btc_df.empty:
            # 補齊所有指標計算以對齊羅盤分數
            btc_df = calculate_technical_indicators(btc_df)
            btc_df = calculate_ahr999(btc_df)
            btc_df = calculate_bear_bottom_indicators(btc_df)

            curr = btc_df.iloc[-1].copy()

            # 升槓桿哨兵用：AHR999 現值（原本只算不外露）。
            # curr 是 Series，缺欄位時 .get 直接回 None；NaN 靠 _a == _a 濾掉。
            _a = curr.get("AHR999")
            summary["ahr999"] = float(_a) if _a is not None and _a == _a else None

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
                    # 升槓桿哨兵用：距 ATH 天數（第二道閘門）
                    _ath_naive = cycle_ath_dt.replace(tzinfo=None)
                    summary["days_since_ath"] = (datetime.now() - _ath_naive).days
                if cycle_ath > 0:
                    summary["from_high_pct"] = (current_price - cycle_ath) / cycle_ath * 100
                    # 熊底確認哨兵 D3 用：自 ATH 之後、且「已跌逾 30%」之後的最低收盤。
                    # 跌幅門檻由 core.leverage_window 統一（BTC_WATCH 走同一個函式），勿在此重寫。
                    try:
                        from core.leverage_window import find_bear_low
                        _after = btc_df[btc_df.index >= _ath_naive] if cycle_ath_dt else None
                        if _after is not None and len(_after):
                            _lo_v, _lo_p = find_bear_low(_after["close"].values, cycle_ath)
                            if _lo_v is not None:
                                _lo_i = _after.index[_lo_p]
                                summary["bear_low_since_ath"] = _lo_v
                                summary["bear_low_date"] = str(_lo_i)[:10]
                                # 距低點天數以日曆天計：cron 執行時當日 K 棒可能還沒收，
                                # 用 K 棒位移會少算一天（BTC_WATCH 手上是完整日線，故用位移）。
                                summary["days_since_bear_low"] = (
                                    datetime.now() - _lo_i.to_pydatetime().replace(tzinfo=None)).days
                    except Exception:
                        pass   # 顯示/提醒用，取不到就讓 D3 哨兵自己 skip，不阻斷主推播

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
            # 套保建倉哨兵（G3）需要數值與「近 90 日是否曾 >75」
            summary["rsi14"] = float(rsi) if rsi else None
            try:
                summary["rsi_max_90d"] = float(btc["RSI_14"].tail(90).max())
            except Exception:
                summary["rsi_max_90d"] = None
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
    from core.action_ensemble import compute_composite_action, CRYPTO_ACTION_NOTES
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

    # 資費歷史（抄底負費率子項的 PiT 分位用）；抓不到回 None → 退回絕對階梯，不影響推播
    try:
        from service.funding_history import funding_ann_hist as _fah
        _funding_hist = _fah(window=400) or None
    except Exception:
        _funding_hist = None
    # 計分用日均資費（與校準同口徑）；取不到沿用即時值。三方（BTC_WATCH/dashboard/本支）一致。
    try:
        from service.funding_history import funding_8h_daily_mean as _f8dm
        _score_funding = _f8dm()
    except Exception:
        _score_funding = None
    if _score_funding is None:
        _score_funding = latest_funding
    common = dict(funding_8h=_score_funding, oi_stats=None, etf_summary=etf,
                  sopr=sopr, fng=fng, btc_d_trend=btcd, macro=macro, mvrv_z=mvrv_z,
                  funding_ann_hist=_funding_hist)
    rh = compute_relative_high(price, curr, btc_df, **common)
    # SOPR / F&G 歷史只有抄底側吃（逃頂側已否決分位法）→ 不放進共用的 common
    try:
        from service.metric_history import sopr_hist as _sh, fng_hist as _fh
        _mh = {'sopr_hist': _sh() or None, 'fng_hist': _fh() or None}
    except Exception:
        _mh = {}
    rl = compute_relative_low(price, curr, btc_df, **common, **_mh)
    td = compute_trend_direction(curr, btc_df)
    ct_state = rh["cycle_top"]

    # 三軸合成行動建議（與 dashboard 同源 core/action_ensemble；傳 cycle 子分補強底部辨識）
    comp = compute_composite_action(td["trend_score"], rh["escape_score"], rl["low_score"],
                                    rl["low_signals"].get("cycle", {}).get("score"),
                                    notes=CRYPTO_ACTION_NOTES)
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
            "composite_pos": comp["pos_label"], "composite_color": comp["color"],
            "composite_note": comp.get("confidence_note")}
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
    逃頂評分 ≥ 門檻時推 LINE 警報，分級見 config.ESCAPE_ALERT_TIERS
    （2026-08-25 由 85/75/60 重校為 51/49/45——舊值全在實測上限 55 之上＝永遠不觸發）。
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


def maybe_send_leverage_window_alert(data: dict, dry_run: bool = False) -> None:
    """升槓桿窗口哨兵（2026-08-23 立）：AHR999 與距 ATH 兩道閘門同時成立才開窗。

    正本：vault「1b 1 BTC ROAD」第八、九節。三種推播：
      1. 窗口開啟（關→開）：發第 1 批指示
      2. 窗口開啟中且距上批 >= LEVERAGE_BATCH_DAYS：發第 N 批（至多 LEVERAGE_BATCH_COUNT 批）
      3. 窗口關閉（開→關）：發收尾，剩餘批次留到下一個窗口（回測：停止優於補完）
    去重與狀態沿用 escape_alert_state.json（同一 artifact 持久化）。
    """
    from config import (LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH,
                        LEVERAGE_BATCH_DAYS, LEVERAGE_BATCH_COUNT)

    from core.leverage_window import gate_status, advance_batches, trigger_price

    gate = gate_status(data.get("ahr999"), data.get("days_since_ath"),
                       LEVERAGE_AHR999_MAX, LEVERAGE_MIN_DAYS_FROM_ATH)
    if gate["ok"] is None:
        print("OK 升槓桿哨兵：AHR999 或距 ATH 天數缺值，略過。")
        return

    state = _load_escape_state()
    ahr, dath = gate["ahr"], gate["dath"]
    today = date.today()
    price = data.get("current_price") or 0
    n = LEVERAGE_BATCH_COUNT
    prev_sent = int(state.get("lev_batches_sent") or 0)
    state, batch, event = advance_batches(
        state, gate["ok"], str(today), LEVERAGE_BATCH_DAYS, n)
    sig = int(state.get("lev_signal_days") or 0)

    def _push(lines: list) -> bool:
        """訊息一律以行陣列組裝，換行交給 join——本檔既有慣用法，
        且原始碼不出現反斜線（本檔曾被反斜線逸出咬壞過）。"""
        if dry_run:
            print(f"[dry-run] 升槓桿哨兵：{lines[0]}（未發送）")
            return True
        try:
            send_line_message({"type": "text", "text": "\n".join(lines)})
            return True
        except Exception as e:
            print(f"X 升槓桿哨兵發送失敗: {e}")
            return False

    # 發送失敗一律就地 return：state 不存檔，下次執行會重試同一批（不會跳批）
    if event == "open" and batch:
        if not _push([
            "[開啟] 升槓桿窗口",
            f"AHR999 {ahr:.3f} < {LEVERAGE_AHR999_MAX}｜距 ATH {dath} 天 >= {LEVERAGE_MIN_DAYS_FROM_ATH}",
            f"BTC ${price:,.0f}",
            "",
            f"投入第 1/{n} 批（每批 1/{n}，間隔 {LEVERAGE_BATCH_DAYS} 個「訊號日」）",
            "配置：2X 無額外保證金，強平價 = 開單價 x 0.667（2026-08-24 拍板）",
            "開單後務必讀 App 實際強平價比對，差 > 1% 立即停止",
            "區間下限 = 前波新低下方；上限 = 下限 x 1.0074^格數（每格 0.74%）",
            "（資金費 30 日均 < 0 的日子歷史均價低 8.3pp，可提前投下一批）",
        ]):
            return
        print(f"! 升槓桿窗口開啟（AHR999 {ahr:.3f}／距 ATH {dath} 天），已發第 1 批。")

    elif event == "reopen":
        print(f"OK 升槓桿窗口重開（訊號日 {sig}，已投 {prev_sent}/{n} 批），批次計數延續不重置。")

    elif event == "batch" and batch:
        if not _push([
            f"[第 {batch}/{n} 批] 升槓桿窗口",
            f"AHR999 {ahr:.3f}｜距 ATH {dath} 天｜BTC ${price:,.0f}",
            f"累計訊號日 {sig}（窗口自 {state.get('lev_window_start')} 開啟；期間關窗不重置）",
            "配置同前：2X 無額外保證金，強平 = 開單價 x 0.667",
        ]):
            return
        print(f"! 升槓桿第 {batch}/{n} 批提醒已發送。")

    elif event == "close":
        why = (f"AHR999 {ahr:.3f} >= {LEVERAGE_AHR999_MAX}"
               if ahr >= LEVERAGE_AHR999_MAX
               else f"距 ATH {dath} 天 < {LEVERAGE_MIN_DAYS_FROM_ATH}")
        if not _push([
            "[暫停] 升槓桿窗口",
            f"{why}｜BTC ${price:,.0f}",
            f"本輪已投 {prev_sent}/{n} 批，累計訊號日 {sig}。",
            f"批次計數保留不歸零：窗口重開會從第 {prev_sent + 1} 批續投。",
            "連續關窗超過 90 天才視為換一個熊市階段、計數歸零。",
        ]):
            return
        print(f"! 升槓桿窗口暫停（{why}），批次計數保留。")

    elif event == "reset":
        print("OK 升槓桿：連續關窗逾 90 天，批次計數歸零。")

    elif gate["ok"]:
        # 窗口開啟中但未到下一批（或六批已發完）——與「窗口未開」語意不同，不可共用文案
        if prev_sent >= n:
            print(f"OK 升槓桿窗口開啟中，{n} 批已發完，不再提醒。")
        else:
            print(f"OK 升槓桿窗口開啟中（訊號日 {sig}／下批需 "
                  f"{prev_sent * LEVERAGE_BATCH_DAYS}），已投 {prev_sent}/{n} 批。")

    else:
        tp = trigger_price(price, ahr, LEVERAGE_AHR999_MAX)
        gap = "" if not tp or not price else f"，需 {(tp / price - 1.0) * 100:+.1f}% -> ${tp:,.0f}"
        print(f"OK 升槓桿窗口未開（AHR999 {ahr:.3f}／距 ATH {dath} 天{gap}）。")

    if not dry_run:
        _save_escape_state(state)


def maybe_send_bear_bottom_confirm_alert(data: dict, dry_run: bool = False) -> None:
    """熊底確認哨兵 D3（2026-08-25 立）：自最低點反彈 >= 50% 且距最低點 >= 90 天。

    判準來源＝`_governance/PREREG-season-falsification.md` 的預簽定義。回測（三次熊底，
    限「自 ATH 已跌逾 30%」後才評判）：中位延遲 +99 天、**提早喊底 0/2**；
    而「站回 200 週均線」「自低點彈 30%」等快訊號提早 51~304 天、觸發價比真底高
    40~197%（2026-05-10 就會在 82,210 喊底，之後跌到 58,625）——故不採用。

    觸發語意：**本輪熊市視為結束 → 升槓桿窗口可能不再開** → 馬丁的 USDT 該考慮換現貨。
    只推一次，狀態存 escape_alert_state.json 的 d3_confirmed。
    """
    from core.leverage_window import d3_status

    lo = data.get("bear_low_since_ath")
    price = data.get("current_price")
    if lo is None or price is None:
        print("OK 熊底確認 D3：低點資料缺值，略過。")
        return
    # cycle_ath 必傳：沒有它就等於少掉 c3「仍在熊市」閘門（2026-08-25 新增）
    d3 = d3_status(price, lo, data.get("bear_low_date"),
                   data.get("days_since_bear_low"),
                   cycle_ath=(data.get("cycle_ath") or None))
    if d3.get("ok") is None:
        print("OK 熊底確認 D3：無法判定，略過。")
        return
    if not d3["ok"]:
        _dd = d3.get("drawdown_from_ath")
        _c3 = "" if d3.get("c3", True) else (
            f"；**c3 未過：距 cycle ATH {_dd * 100:+.1f}%／需 <= "
            f"{d3['drawdown_req'] * 100:.0f}%（仍在 ATH 附近，不視為熊底）**")
        print(f"OK 熊底確認 D3 未達成（反彈 {d3['rebound'] * 100:+.1f}%／需 "
              f"+{d3['rebound_req'] * 100:.0f}%；距低 {d3['days']} 天／需 {d3['days_req']}{_c3}）。")
        return

    state = _load_escape_state()
    if state.get("d3_confirmed"):
        print("OK 熊底確認 D3 已推播過，不重複。")
        return

    lines = [
        "[熊底確認] D3 成立",
        f"自 {d3['low_date']} 低點 ${d3['low']:,.0f} 反彈 {d3['rebound'] * 100:+.1f}%"
        f"（需 +{d3['rebound_req'] * 100:.0f}%）",
        f"距最低點 {d3['days']} 天（需 {d3['days_req']}）｜BTC ${price:,.0f}",
        "",
        "語意：本輪熊市視為結束 -> 升槓桿窗口可能不再開。",
        "待辦：兩台馬丁的 USDT 是為窗口準備的彈藥，此刻應考慮全額換成現貨 BTC。",
        "理由：幣本位計價下現貨是零變異資產；馬丁贏現貨的條件是",
        "「幣價年漲幅 < 馬丁的 USDT 年報酬（中位 +7.8%）」，牛市不成立。",
        "註：這是提醒不是自動執行；PREREG 亦要求同步評判四季論存活與否。",
    ]
    if dry_run:
        print("[dry-run] 熊底確認 D3：" + lines[0] + "（未發送）")
        return
    try:
        send_line_message({"type": "text", "text": "\n".join(lines)})
    except Exception as e:
        print(f"X 熊底確認 D3 發送失敗: {e}")
        return
    state["d3_confirmed"] = True
    state["d3_confirmed_date"] = str(date.today())
    _save_escape_state(state)
    print("! 熊底確認 D3 已推播。")


def maybe_send_hedge_batch_alert(data: dict, dry_run: bool = False) -> None:
    """
    套保分批建倉哨兵（2026-08-25 立）— G3 觸發，分三批。

    規則正本：`Work/BTC幣本位網格去留評估/升槓桿窗口執行清單.md` 附錄 E-1／E-2。
      規模 0.1285 BTC（現貨的一半）｜產品＝**全倉套保**（非分段/網格套保）
      G3 前提：日線 RSI **曾 >75**（近 90 日）後回落
      三批：RSI <65 / <55 / <50，各 0.0428 BTC
      平倉（優先於一切）：AHR999<0.40 開窗，或 D3 熊底確認 → **全部平倉**

    為什麼要哨兵：這三個觸發原本只寫在附錄裡，靠人每天自己看 RSI，漏看就錯過批次。
    每批只推一次，狀態存 escape_alert_state.json 的 hedge_batch_{n}。
    """
    rsi = data.get("rsi14")
    rsi_max = data.get("rsi_max_90d")
    price = data.get("current_price")
    if rsi is None or rsi_max is None:
        print("OK 套保建倉：RSI 資料缺值，略過。")
        return
    if rsi_max <= 75:
        print(f"OK 套保建倉：G3 前提未成立（近 90 日 RSI 最高 {rsi_max:.1f}，需 >75）。")
        return

    state = _load_escape_state()
    batches = [(1, 65, 0.0428), (2, 55, 0.0428), (3, 50, 0.0429)]
    due = [(n, thr, qty) for n, thr, qty in batches
           if rsi < thr and not state.get(f"hedge_batch_{n}")]
    if not due:
        print(f"OK 套保建倉：無新批次（RSI {rsi:.1f}）。")
        return

    n, thr, qty = due[0]          # 一次只推一批，避免同日連推三則
    lines = [
        f"[套保建倉] 第 {n} 批觸發",
        f"日線 RSI {rsi:.1f} < {thr}｜近 90 日曾達 {rsi_max:.1f}（G3 前提成立）",
        f"BTC ${price:,.0f}" if price else "",
        "",
        f"動作：開 {qty} BTC 的**全倉套保**（幣本位 1x 做空）。",
        "不要用分段/網格套保：它是漲上去才逐格賣，跌下來等於沒避險。",
        "",
        "平倉條件（優先於一切）：AHR999<0.40 開窗、或 D3 熊底確認 -> 全部平倉。",
        "用途只有一個：保住開窗時的購買力，不是賺資金費。",
    ]
    if dry_run:
        print(f"[dry-run] 套保建倉第 {n} 批（未發送）")
        return
    try:
        send_line_message({"type": "text",
                           "text": "\n".join(x for x in lines if x != "")})
    except Exception as e:
        print(f"X 套保建倉發送失敗: {e}")
        return
    state[f"hedge_batch_{n}"] = True
    state[f"hedge_batch_{n}_date"] = str(date.today())
    _save_escape_state(state)
    print(f"! 套保建倉第 {n} 批已推播。")


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
    # 升槓桿窗口：AHR999 + 距 ATH 兩道閘門，開/關/分批提醒（2026-08-23）
    maybe_send_leverage_window_alert(data)
    maybe_send_bear_bottom_confirm_alert(data)
    maybe_send_hedge_batch_alert(data)
    # 週報：週日傍晚場次加推一則（每週一次）
    maybe_send_weekly_summary(data)
