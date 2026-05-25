"""
handler/components/macro_utils.py

長週期羅盤 (Macro Compass) 共用工具函數。

由 handler/tab_macro_compass.py 引用，
取代原本 macro_bottom.py / macro_charts.py / macro_score.py
三個重複實驗版本（已刪除）。
"""
import plotly.graph_objects as go


# ══════════════════════════════════════════════════════════════════════════════
# 評分等級轉換
# ══════════════════════════════════════════════════════════════════════════════

def _score_meta(score: int):
    """將 -100~+100 市場評分轉換為等級標籤、顏色與操作建議。"""
    if score >= 75:
        return "🔥 狂熱牛頂", "#ff4b4b", "風險極高，建議分批止盈。此區域歷史上出現牛市最終頂部。"
    elif score >= 40:
        return "🐂 牛市主升段", "#ff9800", "趨勢多頭排列，可持有並設移動止盈，避免頂部追高。"
    elif score >= 15:
        return "🌱 初牛復甦", "#8bc34a", "市場轉暖，分批建倉機會。等待黃金交叉與年線翻揚確認。"
    elif score >= -15:
        return "⚪ 中性過渡", "#9e9e9e", "多空力量均衡，觀望為主，等待方向確認。"
    elif score >= -40:
        return "📉 轉折回調", "#7986cb", "跌破關鍵均線，趨勢轉弱，建議輕倉或觀望。"
    elif score >= -75:
        return "❄️ 熊市築底", "#42a5f5", "熊市中後期，多指標出現底部信號，開始定投積累。"
    else:
        return "🟦 歷史極值底部", "#00bcd4", "All-In 信號！歷史上極為罕見的買入機會，建議全力積累。"


def _bear_score_meta(score: int):
    """0-100 熊市底部評分 → 標籤、顏色、建議。"""
    if score >= 75:
        return "🔴 歷史極值底部", "#ff4444", "All-In 信號！建議全力積累。"
    elif score >= 60:
        return "🟠 明確底部區間", "#ff8800", "積極積累區，建議重倉布局。"
    elif score >= 45:
        return "🟡 可能底部區",   "#ffcc00", "謹慎試探，建議小倉分批試探。"
    elif score >= 25:
        return "⚪ 震盪修正區",   "#aaaaaa", "觀望為主，尚未出現明確底部信號。"
    else:
        return "🟢 牛市/高估區",  "#00ff88", "非底部時機，持有或減倉。"


# ══════════════════════════════════════════════════════════════════════════════
# 油錶圖
# ══════════════════════════════════════════════════════════════════════════════

def _build_cycle_gauge(market_score: int) -> go.Figure:
    """
    市場多空油錶圖 (-100 到 +100)。
    6 個相位色塊從深熊到狂熱頂部。
    """
    _, color, _ = _score_meta(market_score)

    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=market_score,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={
            'text': "市場多空評分<br><span style='font-size:0.75em;color:gray'>Cycle Score (-100 → +100)</span>",
            'font': {'size': 18},
        },
        delta={'reference': 0, 'increasing': {'color': '#ff9800'}, 'decreasing': {'color': '#42a5f5'}},
        gauge={
            'axis': {
                'range': [-100, 100],
                'tickvals': [-100, -75, -40, -15, 0, 15, 40, 75, 100],
                'ticktext': ['-100\n極深熊', '-75', '-40', '-15', '0\n中性', '+15', '+40', '+75', '+100\n狂熱頂'],
                'tickwidth': 1, 'tickcolor': 'white',
            },
            'bar': {'color': color, 'thickness': 0.25},
            'bgcolor': '#1e1e1e',
            'borderwidth': 2, 'bordercolor': '#333',
            'steps': [
                {'range': [-100, -75], 'color': '#0d2044'},
                {'range': [-75, -40],  'color': '#0d3560'},
                {'range': [-40, -15],  'color': '#1a2a50'},
                {'range': [-15, 15],   'color': '#2a2a2a'},
                {'range': [15, 40],    'color': '#1a3a1a'},
                {'range': [40, 75],    'color': '#2a3a10'},
                {'range': [75, 100],   'color': '#3a1a10'},
            ],
            'threshold': {
                'line': {'color': 'white', 'width': 3},
                'thickness': 0.75, 'value': market_score,
            },
        },
    ))
    fig.update_layout(
        height=280, template="plotly_dark",
        paper_bgcolor="#0e1117", font={'color': 'white'},
        margin=dict(l=20, r=20, t=60, b=10),
    )
    return fig


def _build_phase_gauge(phase_score: int, phase_name: str) -> go.Figure:
    """
    市場相位油錶 (0-5 相位，go.Indicator)。
    將 6 個相位對應到 0-5 刻度（深熊→頂部）。
    """
    phases = [
        "❄️ 深熊築底",
        "📉 轉折回調",
        "🌱 初牛復甦",
        "😴 牛市休整/末期",
        "🐂 牛市主升段",
        "🔥 狂熱頂部",
    ]
    phase_score = max(0, min(phase_score, len(phases) - 1))
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=phase_score,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={
            'text': f"市場相位<br><span style='font-size:0.8em;color:#aaa'>{phase_name}</span>",
            'font': {'size': 14},
        },
        number={'suffix': f"/{len(phases)-1}", 'font': {'size': 24}},
        gauge={
            'axis': {
                'range': [0, 5],
                'tickvals': list(range(6)),
                'ticktext': ["深熊", "回調", "初牛", "牛休", "主升", "頂部"],
                'tickwidth': 1, 'tickcolor': 'white',
            },
            'bar': {
                'color': ['#42a5f5', '#7986cb', '#8bc34a', '#ffd54f', '#ff9800', '#ff4b4b'][phase_score],
                'thickness': 0.3,
            },
            'bgcolor': '#1e1e1e',
            'borderwidth': 2, 'bordercolor': '#333',
            'steps': [
                {'range': [0, 1], 'color': '#0d2044'},
                {'range': [1, 2], 'color': '#1a2a50'},
                {'range': [2, 3], 'color': '#1a3a1a'},
                {'range': [3, 4], 'color': '#2a3a10'},
                {'range': [4, 5], 'color': '#3a3a10'},
            ],
        },
    ))
    fig.update_layout(
        height=240, template="plotly_dark",
        paper_bgcolor="#0e1117", font={'color': 'white'},
        margin=dict(l=10, r=10, t=60, b=10),
    )
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# 四季顏色
# ══════════════════════════════════════════════════════════════════════════════

def _season_css_color(season: str) -> str:
    """四季 → CSS 顏色（春綠 / 夏黃 / 秋橙 / 冬藍）。"""
    return {
        "spring": "#00e676",
        "summer": "#ffeb3b",
        "autumn": "#ff9800",
        "winter": "#42a5f5",
    }.get(season, "#ffffff")
