"""
scripts/test_flex_message.py
1. 補齊 bear_bottom_indicators 計算，確保分數對齊羅盤 (-84分)。
2. 保留目標價預測區塊，隨市場狀態自動切換牛/熊標題。
3. MA200 增加與現價對比標籤。
4. 資金費率採 0.03% 過熱標準並加燈號。
5. 更新綜合波段建議邏輯。
"""

import os
import sys
import urllib3
import requests
from core.http_client import safe_get, safe_post
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# 強制關閉 SSL 驗證
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
os.environ['CURL_CA_BUNDLE'] = ''
os.environ['REQUESTS_CA_BUNDLE'] = ''


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_REPO_ROOT, '.env'))

from service.realtime import fetch_realtime_data
from service.market_data import fetch_market_data
from service.local_db_reader import read_btc_daily
from service.onchain import _fetch_funding_rate_history
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators, calculate_market_cycle_score
from core.season_forecast import forecast_price

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
        "price": "N/A", "cycle_score": 0, "cycle_name": "N/A", "cycle_color": "#aaaaaa", "cycle_advice": "",
        "ma200_label": "N/A", "funding_text": "N/A", "funding_color": "#aaaaaa",
        "trend_text": "N/A", "trend_color": "#aaaaaa",
        "rsi_text": "N/A", "rsi_color": "#aaaaaa",
        "macd_text": "N/A", "macd_color": "#aaaaaa",
        "adx_text": "N/A", "adx_color": "#aaaaaa",
        "ema_dist_text": "N/A", "ema_dist_color": "#aaaaaa",
        "swing_advice": "N/A", "swing_advice_color": "#aaaaaa",
        "forecast_type": "bear_bottom", "target_low": 0, "target_median": 0, "target_high": 0,
        "label_low": "最深", "label_high": "最淺"
    }
    
    current_price = None
    try:
        realtime_data = fetch_realtime_data()
        if realtime_data and realtime_data.get('price'):
            current_price = float(realtime_data['price'])
    except: pass

    try:
        btc_df, _ = fetch_market_data()
        if btc_df is None or btc_df.empty: btc_df = read_btc_daily()
        
        funding_df = _fetch_funding_rate_history()
        latest_funding = 0.0
        if not funding_df.empty:
            latest_funding = funding_df['fundingRate'].iloc[-1]
            f_is_hot = latest_funding >= 0.03 # 資金費率過熱標準 0.03%
            summary["funding_text"] = f"{'🔴' if f_is_hot else '🟢'} {latest_funding:.4f}%"
            summary["funding_color"] = "#ff4b4b" if f_is_hot else "#00ff88"

        if not btc_df.empty:
            btc_df = calculate_technical_indicators(btc_df)
            btc_df = calculate_ahr999(btc_df)
            btc_df = calculate_bear_bottom_indicators(btc_df)
            
            curr = btc_df.iloc[-1].copy()
            if current_price is None:
                current_price = float(curr['close'])
            
            curr['close'] = current_price
            summary["price"] = f"${current_price:,.0f}"
            curr['funding_rate'] = latest_funding
            
            # MA200 狀態
            ma200 = curr.get('SMA_200', 0)
            ma_is_higher = ma200 > current_price
            summary["ma200_label"] = f"{'🔴' if ma_is_higher else '🟢'} ${ma200:,.0f} ({'>' if ma_is_higher else '<'} 現價)"

            # 預測區塊
            f_res = forecast_price(current_price, btc_df)
            if f_res:
                summary.update({
                    "forecast_type": f_res["forecast_type"],
                    "target_low": f_res["target_low"], "target_median": f_res["target_median"], "target_high": f_res["target_high"],
                    "label_low": "最深" if "bear" in f_res["forecast_type"] else "保守",
                    "label_high": "最淺" if "bear" in f_res["forecast_type"] else "樂觀"
                })

            # 長週期評分
            score = calculate_market_cycle_score(curr)
            summary["cycle_score"] = score
            summary["cycle_name"], summary["cycle_color"], summary["cycle_advice"] = _get_cycle_meta(score)

            # 波段雷達數值
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

            # 綜合波段建議 (根據您的邏輯更新)
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

    except Exception as e: print(f"Error: {e}")
    return summary

def build_flex_message(s):
    left_flex = max(1, min(99, int((s["cycle_score"] + 100) / 2)))
    forecast_title = "❄️ 熊市最低價預測" if s["forecast_type"] == "bear_bottom" else "🚀 牛市最高價預測"
    
    return {
        "type": "flex", "altText": f"🦅 決策速報: BTC {s['price']}",
        "contents": {
            "type": "bubble", "size": "giga",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#191919",
                "contents": [{"type": "text", "text": "🦅 戰情室決策速報", "weight": "bold", "color": "#ffffff", "size": "xl"}]
            },
            "body": {
                "type": "box", "layout": "vertical", "backgroundColor": "#222222",
                "contents": [
                    {"type": "text", "text": f"💰 BTC {s['price']}", "weight": "bold", "size": "xxl", "color": "#00ff88"},
                    {"type": "separator", "margin": "md", "color": "#444444"},
                    {"type": "text", "text": "🧭 長週期多空評分", "weight": "bold", "color": "#ffffff", "margin": "md"},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "box", "layout": "vertical", "flex": 7, "contents": [
                            {"type": "text", "text": s["cycle_name"], "color": s["cycle_color"], "weight": "bold", "size": "md"},
                            {"type": "text", "text": s["cycle_advice"], "color": "#aaaaaa", "size": "xs", "wrap": True}
                        ]},
                        {"type": "box", "layout": "vertical", "flex": 3, "alignItems": "flex-end", "contents": [
                            {"type": "text", "text": f"{s['cycle_score']:+d}", "color": s["cycle_color"], "size": "xxl", "weight": "bold"}
                        ]}
                    ]},
                    {"type": "box", "layout": "horizontal", "margin": "md", "height": "8px", "contents": [
                        {"type": "box", "layout": "vertical", "flex": left_flex, "backgroundColor": s["cycle_color"], "contents": []},
                        {"type": "box", "layout": "vertical", "flex": 100-left_flex, "backgroundColor": "#444444", "contents": []}
                    ]},
                    # 目標價預測區塊
                    {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#2a2a2a", "paddingAll": "md", "cornerRadius": "8px", "contents": [
                        {"type": "text", "text": forecast_title, "color": "#ffffff", "size": "xs", "weight": "bold"},
                        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                            {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": s["label_low"], "color": "#aaaaaa", "size": "xxs", "align": "center"},{"type": "text", "text": f'${s["target_low"]:,.0f}', "color": "#ffffff", "size": "xs", "align": "center"}]},
                            {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "中位數", "color": "#00ff88", "size": "xxs", "align": "center"},{"type": "text", "text": f'${s["target_median"]:,.0f}', "color": "#00ff88", "size": "sm", "weight": "bold", "align": "center"}]},
                            {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": s["label_high"], "color": "#aaaaaa", "size": "xxs", "align": "center"},{"type": "text", "text": f'${s["target_high"]:,.0f}', "color": "#ffffff", "size": "xs", "align": "center"}]}
                        ]}
                    ]},
                    {"type": "separator", "margin": "lg", "color": "#444444"},
                    # 波段雷達
                    {"type": "text", "text": "🐂 波段雷達", "weight": "bold", "color": "#ffffff", "margin": "md"},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "MA200 支撐", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["ma200_label"], "color": "#ffffff", "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "資金費率", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["funding_text"], "color": s["funding_color"], "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "趨勢方向", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["trend_text"], "color": s["trend_color"], "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "RSI 強弱", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["rsi_text"], "color": s["rsi_color"], "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "MACD 交叉", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["macd_text"], "color": s["macd_color"], "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "ADX 動能", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["adx_text"], "color": s["adx_color"], "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": "EMA20 乖離", "color": "#aaaaaa", "size": "sm"},{"type": "text", "text": s["ema_dist_text"], "color": s["ema_dist_color"], "size": "sm", "weight": "bold", "align": "end"}]},
                    {"type": "box", "layout": "vertical", "margin": "lg", "backgroundColor": "#1a1a1a", "paddingAll": "md", "cornerRadius": "8px", "contents": [
                        {"type": "text", "text": "💡 策略建議", "color": "#888888", "size": "xxs", "weight": "bold"},
                        {"type": "text", "text": s["swing_advice"], "color": s["swing_advice_color"], "size": "xs", "weight": "bold", "wrap": True}
                    ]}
                ]
            }
        }
    }

def send_test_message(flex):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    if token and user_id:
        safe_post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {token}"}, json={"to": user_id, "messages": [flex]}, verify=False)
        print("✅ 測試發送成功")

if __name__ == "__main__":
    data = get_decision_data()
    send_test_message(build_flex_message(data))