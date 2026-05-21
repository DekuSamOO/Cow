"""
handler/tab_macro_compass.py  ·  v1.0
長週期週期羅盤 (Macro Cycle Compass)

整合原 Tab 1 牛市雷達 + Tab 5 熊市底部獵人，提供完整的長週期宏觀視角：
  1. 市場多空評分儀表 (-100 到 +100 油錶圖)
  2. 市場相位油錶 (6 個相位，go.Indicator)
  3. 多維度長週期主圖 (Price + AHR999 + Funding + TVL + Stablecoin)
  4. 指標評分卡片化 (Level 1-3 Card Layout)
  5. 熊市底部獵人分析 (8 大指標 + 底部驗證圖)
  6. 四季理論目標價預測

Session State 快取：
  - 主圖表 (tab_mc_fig_main_<hash>)
  - 底部驗證圖 (tab_mc_fig_hist_<hash>)
  - 評分走勢圖 (tab_mc_fig_score_<hash>)
  - 預測圖 (tab_mc_fig_fc_<hash>)
"""
import hashlib
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
from datetime import datetime

from service.macro_data import fetch_m2_series, fetch_usdjpy, fetch_us_cpi_yoy, get_quantum_threat_level
from core.bear_bottom import (
    calculate_bear_bottom_score,
    calculate_market_cycle_score,
    calculate_market_cycle_score_breakdown,
    score_series,
)
from core.season_forecast import (
    forecast_price,
    get_cycle_comparison_table,
    get_power_law_forecast,
    get_current_season,
    HALVING_DATES,
    CYCLE_HISTORY,
)

# ── Fallback 靜態數據（macro_data 連線失敗時使用）─────────────────────────────
_FALLBACK = {
    "dxy":    {"value": 106.5,  "date": "2025-02-21"},
    "m2":     {"value": 21450,  "date": "2025-01-01"},
    "cpi":    {"value": 3.0,    "date": "2025-01-01"},
    "usdjpy": {"value": 150.5,  "date": "2025-02-21"},
}

# 歷史已知熊市底部區間
KNOWN_BOTTOMS = [
    ("2015-08-01", "2015-09-30", "2015 Bear Bottom"),
    ("2018-11-01", "2019-02-28", "2018-19 Bear Bottom"),
    ("2020-03-01", "2020-04-30", "2020 COVID Crash"),
    ("2022-11-01", "2023-01-31", "2022 FTX Bear Bottom"),
]


# ══════════════════════════════════════════════════════════════════════════════
# 快取鍵
# ══════════════════════════════════════════════════════════════════════════════

def _make_mc_cache_key(chart_df, tvl_hist, stable_hist, fund_hist) -> str:
    parts = [
        str(chart_df.index[-1])    if not chart_df.empty    else "empty",
        str(len(chart_df)),
        str(tvl_hist.index[-1])    if not tvl_hist.empty    else "empty",
        str(stable_hist.index[-1]) if not stable_hist.empty else "empty",
        str(fund_hist.index[-1])   if not fund_hist.empty   else "empty",
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]


def _make_bb_cache_key(btc: pd.DataFrame) -> str:
    last_idx = str(btc.index[-1]) if not btc.empty else "empty"
    return hashlib.md5(f"{last_idx}|{len(btc)}".encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
# 評分工具函數
# ══════════════════════════════════════════════════════════════════════════════

def _score_meta(score: int):
    """將 -100~+100 市場評分轉換為等級標籤與顏色"""
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
    """0-100 底部評分 → 標籤、顏色、建議"""
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
    市場多空油錶圖 (-100 到 +100)
    6 個相位色塊從深熊到狂熱頂部。
    """
    level, color, _ = _score_meta(market_score)

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
                {'range': [-100, -75], 'color': '#0d2044'},   # 歷史極值底部
                {'range': [-75, -40],  'color': '#0d3560'},   # 熊市築底
                {'range': [-40, -15],  'color': '#1a2a50'},   # 轉折回調
                {'range': [-15, 15],   'color': '#2a2a2a'},   # 中性
                {'range': [15, 40],    'color': '#1a3a1a'},   # 初牛復甦
                {'range': [40, 75],    'color': '#2a3a10'},   # 牛市主升
                {'range': [75, 100],   'color': '#3a1a10'},   # 狂熱頂部
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
    市場相位油錶 (0-6 相位，go.Indicator)
    將 6 個相位對應到 0-6 刻度。
    """
    phases = [
        "❄️ 深熊築底",
        "📉 轉折回調",
        "🌱 初牛復甦",
        "😴 牛市休整/末期",
        "🐂 牛市主升段",
        "🔥 狂熱頂部",
    ]
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
            'bar': {'color': ['#42a5f5','#7986cb','#8bc34a','#ffd54f','#ff9800','#ff4b4b'][phase_score], 'thickness': 0.3},
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
# Section F 輔助函數（來自 tab_bear_bottom）
# ══════════════════════════════════════════════════════════════════════════════

def _season_css_color(season: str) -> str:
    return {
        "spring": "#00e676",
        "summer": "#ffeb3b",
        "autumn": "#ff9800",
        "winter": "#42a5f5",
    }.get(season, "#ffffff")


def _render_season_timeline(season_info: dict, effective_season: str = None):
    fig = go.Figure()
    season_keys   = ["spring", "summer", "autumn", "winter"]
    season_colors = ["#1b5e20", "#f9a825", "#e65100", "#0d47a1"]
    season_labels = ["🌱 春 (月0-11)", "☀️ 夏 (月12-23)", "🍂 秋 (月24-35)", "❄️ 冬 (月36-47)"]

    for i, (key, col, lab) in enumerate(zip(season_keys, season_colors, season_labels)):
        is_eff = (effective_season == key) and (effective_season != season_info["season"])
        fig.add_shape(
            type="rect", x0=i*12, x1=(i+1)*12, y0=0, y1=1,
            fillcolor=col, opacity=0.7 if is_eff else 0.35, layer="below",
            line=dict(color="#ffffff", width=3) if is_eff else dict(width=0),
        )
        fig.add_annotation(
            x=i*12+6, y=0.5,
            text=lab + (" ← 實際" if is_eff else ""),
            showarrow=False,
            font=dict(size=11, color="white"),
        )

    m = season_info["month_in_cycle"]
    fig.add_shape(type="line", x0=m, x1=m, y0=0, y1=1, line=dict(color="#ffffff", width=3))
    fig.add_annotation(x=m, y=1.1, text=f"現在 (月{m})", showarrow=False, font=dict(size=12, color="white"))
    fig.update_layout(
        height=130, margin=dict(l=10, r=10, t=35, b=10), template="plotly_dark",
        xaxis=dict(range=[0,48], showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(range=[0,1.25], showticklabels=False, showgrid=False, zeroline=False),
        paper_bgcolor="#0e1117", plot_bgcolor="#0e1117",
    )
    return fig


def _render_forecast_chart(btc: pd.DataFrame, fc: dict):
    hist_2y   = btc.tail(365*2)
    future_pl = get_power_law_forecast(btc, months_ahead=12)
    is_bull   = fc["forecast_type"] == "bull_peak"
    ribbon_color = "rgba(255,235,59,0.18)" if is_bull else "rgba(66,165,245,0.18)"
    median_color = "#ffeb3b" if is_bull else "#42a5f5"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(future_pl.index)+list(future_pl.index[::-1]),
        y=list(future_pl["upper"])+list(future_pl["lower"][::-1]),
        fill="toself", fillcolor="rgba(255,204,0,0.07)",
        line=dict(color="rgba(0,0,0,0)"), name="冪律走廊",
    ))
    fig.add_trace(go.Scatter(
        x=future_pl.index, y=future_pl["median"],
        mode="lines", line=dict(color="#ffcc00", width=1, dash="dot"), name="冪律中線",
    ))
    fig.add_trace(go.Scatter(
        x=hist_2y.index, y=hist_2y["close"],
        mode="lines", name="BTC 歷史收盤", line=dict(color="#ffffff", width=2),
    ))

    est_date = fc["estimated_date"]
    today    = datetime.utcnow()
    ribbon_x = [today, est_date, est_date, today]
    ribbon_y  = [fc["target_high"]]*2 + [fc["target_low"]]*2
    fig.add_trace(go.Scatter(
        x=ribbon_x+[today], y=ribbon_y+[fc["target_high"]],
        fill="toself", fillcolor=ribbon_color,
        line=dict(color="rgba(0,0,0,0)"), name="目標價區間",
    ))
    fig.add_shape(
        type="line", x0=today, x1=est_date,
        y0=fc["target_median"], y1=fc["target_median"],
        line=dict(color=median_color, width=2.5, dash="dash"),
    )
    label = "🎯 牛市目標高點" if is_bull else "🎯 熊市目標低點"
    fig.add_annotation(
        x=est_date, y=fc["target_median"],
        text=f"{label}<br>${fc['target_median']:,.0f}",
        showarrow=True, arrowhead=2,
        font=dict(color=median_color, size=12),
        bgcolor="#1e1e1e", bordercolor=median_color, borderwidth=1,
    )
    for val, clr, lbl in [
        (fc["target_high"], "#ff9800", "樂觀目標"),
        (fc["target_low"],  "#78909c", "保守目標"),
    ]:
        fig.add_shape(
            type="line", x0=today, x1=est_date, y0=val, y1=val,
            line=dict(color=clr, width=1.2, dash="dot"),
        )
        fig.add_annotation(
            x=est_date, y=val, text=f"{lbl}: ${val:,.0f}",
            showarrow=False, xanchor="left", font=dict(color=clr, size=10),
        )
    fig.add_shape(
        type="line", x0=today, x1=today, y0=0, y1=1,
        xref="x", yref="paper", line=dict(color="#888888", width=1, dash="dash"),
    )
    fig.update_layout(
        height=500, template="plotly_dark", yaxis_type="log",
        title=dict(text=f"{'📈 牛市最高價' if is_bull else '📉 熊市最低價'} 預測 — 未來 12 個月", font=dict(size=16)),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
        paper_bgcolor="#0e1117",
    )
    return fig


def _render_cycle_waterfall(fc: dict):
    from core.season_forecast import STATS
    labels, values, colors, bar_texts = [], [], [], []
    for i, c in enumerate(CYCLE_HISTORY):
        yr = c["halving"].year
        if c["is_complete"]:
            labels.append(f"第{i+1}週期\n({yr})")
            values.append(c["peak_mult"])
            colors.append("#ff9800")
            bar_texts.append(f"{c['peak_mult']:.1f}x")
        else:
            labels.append(f"第{i+1}週期\n({yr}) 進行中")
            values.append(c["peak_mult"])
            colors.append("#42a5f5")
            bar_texts.append(f"{c['peak_mult']:.2f}x ✓\n(ATH已達)")

    fig = go.Figure(go.Bar(
        x=labels, y=values, marker_color=colors, text=bar_texts, textposition="outside",
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=values, mode="lines+markers",
        line=dict(color="#ffffff", width=1.5, dash="dot"), showlegend=False,
    ))
    fig.update_layout(
        height=320, template="plotly_dark",
        title="歷史牛市漲幅遞減規律（相對減半時價格）",
        yaxis_title="倍數 (x)", paper_bgcolor="#0e1117", showlegend=False,
        annotations=[dict(
            text="🔵 進行中 = ATH倍數已確認，熊市底部尚未完成",
            xref="paper", yref="paper", x=0, y=-0.15,
            showarrow=False, font=dict(size=10, color="#42a5f5"), align="left",
        )],
    )
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# 主渲染函數
# ══════════════════════════════════════════════════════════════════════════════

def render(btc, chart_df, tvl_hist, stable_hist, fund_hist,
           curr, dxy, funding_rate, tvl_val,
           fng_val, fng_state, fng_source, proxies, realtime_data):
    """
    長週期週期羅盤 (Macro Cycle Compass)

    整合 Tab 1 (牛市雷達) + Tab 5 (熊市底部獵人)，
    提供從短週期技術面到長週期鏈上指標的完整宏觀視角。
    """
    st.subheader("🧭 長週期羅盤 (Macro Cycle Compass)")
    st.caption("整合長週期技術指標、鏈上數據與宏觀環境，量化市場所處的週期位置")

    # ══════════════════════════════════════════════════════════════
    # Section 0: 市場多空評分儀表
    # ══════════════════════════════════════════════════════════════
    market_score, _bear_total, _bull_total, _breakdown_rows = calculate_market_cycle_score_breakdown(curr)
    bear_score_now, _ = calculate_bear_bottom_score(curr)

    # 確定市場相位 (0-5)
    price        = curr['close']
    ma50         = curr.get('SMA_50', price)
    ma200        = curr.get('SMA_200', price)
    ma200_slope  = curr.get('SMA_200_Slope', 0) or 0
    mvrv         = curr.get('MVRV_Z_Proxy', 0) or 0

    if mvrv > 3.5:
        phase_idx, phase_name, phase_desc = 5, "🔥 狂熱頂部", "風險極高，建議分批止盈。MVRV Z > 3.5 歷史頂部信號。"
    elif price > ma200 and ma50 > ma200 and ma200_slope > 0:
        phase_idx, phase_name, phase_desc = 4, "🐂 牛市主升段", "多頭排列，年線上揚。策略：持有並設移動止盈。"
    elif price > ma200 and ma50 > ma200 and ma200_slope <= 0:
        phase_idx, phase_name, phase_desc = 3, "😴 牛市休整/末期", "價格高於年線但動能減弱。策略：輕倉持有，注意反轉。"
    elif price > ma200 and ma50 <= ma200:
        phase_idx, phase_name, phase_desc = 2, "🌱 初牛復甦", "站上年線，等待黃金交叉。策略：分批建倉。"
    elif price <= ma200 and ma50 > ma200:
        phase_idx, phase_name, phase_desc = 1, "📉 轉折回調", "跌破年線，注意死叉風險。策略：輕倉觀望。"
    else:
        phase_idx, phase_name, phase_desc = 0, "❄️ 深熊築底", "均線空頭排列，底部積累區。策略：定投囤幣。"

    level_name, level_color, level_action = _score_meta(market_score)

    # 評分橫幅
    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            border: 2px solid {level_color};
            border-radius: 14px;
            padding: 20px 28px;
            margin-bottom: 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
        ">
            <div>
                <div style="color:{level_color};font-size:1.8rem;font-weight:800;">{level_name}</div>
                <div style="color:#ccc;font-size:0.9rem;margin-top:4px;">{level_action}</div>
            </div>
            <div style="text-align:right;">
                <div style="color:#aaa;font-size:0.8rem;">多空評分</div>
                <div style="color:{level_color};font-size:3rem;font-weight:900;line-height:1;">{market_score:+d}</div>
                <div style="color:#666;font-size:0.75rem;">-100 (深熊) → +100 (狂熱)</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 雙油錶
    g_col1, g_col2, g_col3 = st.columns([2, 2, 3])
    with g_col1:
        st.plotly_chart(_build_cycle_gauge(market_score), use_container_width=True)
    with g_col2:
        st.plotly_chart(_build_phase_gauge(phase_idx, phase_name), use_container_width=True)
    with g_col3:
        st.markdown(f"### 📡 {phase_name}")
        st.info(phase_desc)
        st.markdown("""
        | 相位 | 描述 | 策略建議 |
        |------|------|---------|
        | 🔥 狂熱頂部 | MVRV Z > 3.5 | 分批止盈 |
        | 🐂 牛市主升 | 多頭排列+年線上揚 | 持有止盈 |
        | 😴 牛市末期 | 多頭但動能減弱 | 輕倉持有 |
        | 🌱 初牛復甦 | 站上年線 | 分批建倉 |
        | 📉 轉折回調 | 跌破年線 | 觀望為主 |
        | ❄️ 深熊築底 | 空頭排列 | 定投積累 |
        """)

    # ── 多空評分公式說明 expander ──────────────────────────────────────────────
    with st.expander(
        f"📐 多空評分計算公式（熊底 {_bear_total}/100 分 — 牛頂 {_bull_total}/100 分 = **{market_score:+d}**）",
        expanded=False,
    ):
        st.caption(
            "**公式**：多空評分 = 牛頂分數 − 熊底分數，clip 至 [-100, +100]。"
            "8 大鏈上指標各自對熊底與牛頂分別打分，分數根據最新日線即時計算。"
            "若分數長時間不變，屬正常現象（代表市場週期位置確實穩定在當前區間，非 bug）。"
        )
        _tbl = []
        for _r in _breakdown_rows:
            _net = _r['bull'] - _r['bear']
            _tbl.append({
                '指標': _r['name'],
                '當前值': _r['value'],
                f"熊底分 (/{_r['bear_max']})": _r['bear'],
                f"牛頂分 (/{_r['bull_max']})": _r['bull'],
                '淨貢獻 (牛-熊)': f"{_net:+d}",
            })
        st.dataframe(pd.DataFrame(_tbl), use_container_width=True, hide_index=True)
        st.caption(
            f"合計 → 熊底 {_bear_total} 分 ｜ 牛頂 {_bull_total} 分 ｜"
            f" 最終分數 = {_bull_total} − {_bear_total} = **{market_score:+d}**"
        )

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════
    # Section 1: 多維度長週期主圖表（含快取）
    # ══════════════════════════════════════════════════════════════
    st.subheader("A. 多維度長週期主圖 (BTC Price + On-Chain)")

    cache_key   = _make_mc_cache_key(chart_df, tvl_hist, stable_hist, fund_hist)
    ss_hash_key = "tab_mc_hash"
    ss_main_key = f"tab_mc_fig_main_{cache_key}"

    if st.session_state.get(ss_hash_key) == cache_key and ss_main_key in st.session_state:
        fig_main = st.session_state[ss_main_key]
    else:
        _cdf = chart_df.copy()
        if _cdf.index.tz is not None:
            _cdf.index = _cdf.index.tz_localize(None)

        fig_main = make_subplots(
            rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.025,
            row_heights=[0.40, 0.15, 0.15, 0.15, 0.15],
            subplot_titles=(
                "比特幣價格行為 + MA200 / MA50 (Price Action)",
                "AHR999 囤幣指標 (< 0.45 = 歷史抄底區)",
                "幣安資金費率 (Funding Rate) & RSI_14",
                "BTC 鏈上 TVL (DeFiLlama)",
                "全球穩定幣市值 (Stablecoin Cap)",
            ),
        )

        # Row 1: 價格 + 均線（MA200 + MA50 都畫出，與 Level 1 邏輯完全對應）
        fig_main.add_trace(go.Candlestick(
            x=_cdf.index, open=_cdf['open'], high=_cdf['high'],
            low=_cdf['low'], close=_cdf['close'], name='BTC',
        ), row=1, col=1)
        fig_main.add_trace(go.Scatter(
            x=_cdf.index, y=_cdf['SMA_200'],
            line=dict(color='orange', width=2), name='SMA 200',
        ), row=1, col=1)
        fig_main.add_trace(go.Scatter(
            x=_cdf.index, y=_cdf['SMA_50'],
            line=dict(color='cyan', width=1.5, dash='dash'), name='SMA 50',
        ), row=1, col=1)
        if 'EMA_20' in _cdf.columns:
            fig_main.add_trace(go.Scatter(
                x=_cdf.index, y=_cdf['EMA_20'],
                line=dict(color='#ffeb3b', width=1, dash='dot'), name='EMA 20',
            ), row=1, col=1)

        # Row 2: AHR999
        if 'AHR999' in _cdf.columns and _cdf['AHR999'].notna().any():
            ahr_c = ['#00ff88' if v < 0.45 else ('#ffcc00' if v < 0.8 else ('#ff8800' if v < 1.2 else '#ff4b4b'))
                     for v in _cdf['AHR999'].fillna(1.0)]
            fig_main.add_trace(go.Bar(
                x=_cdf.index, y=_cdf['AHR999'], marker_color=ahr_c, name='AHR999', showlegend=False,
            ), row=2, col=1)
            for lvl, col, lbl in [(0.45,'#00ff88','抄底 0.45'),(0.8,'#ffcc00','偏低 0.8'),(1.2,'#ff4b4b','高估 1.2')]:
                fig_main.add_hline(y=lvl, line_color=col, line_width=1, line_dash='dash',
                                   annotation_text=lbl, row=2, col=1)

        # Row 3: 資金費率 + RSI
        if not fund_hist.empty:
            fund_sub = fund_hist.reindex(_cdf.index, method='nearest')
            # 資金費率歷史只從 2021 年起有資料（Binance 永續合約），
            # reindex(nearest) 會將更早的日期全部填為第一筆定值，需清除
            fund_sub.loc[fund_sub.index < fund_hist.index[0]] = np.nan
            valid_mask = fund_sub['fundingRate'].notna()
            fr_colors = ['#00ff88' if v > 0 else '#ff4b4b'
                         for v in fund_sub.loc[valid_mask, 'fundingRate']]
            fig_main.add_trace(go.Bar(
                x=fund_sub.index[valid_mask],
                y=fund_sub.loc[valid_mask, 'fundingRate'],
                marker_color=fr_colors, name='Funding Rate %',
            ), row=3, col=1)
        if 'RSI_14' in _cdf.columns and _cdf['RSI_14'].notna().any():
            fig_main.add_trace(go.Scatter(
                x=_cdf.index, y=(_cdf['RSI_14'] - 50) * 0.001,
                line=dict(color='#a32eff', width=1.5), name='RSI (scaled)',
            ), row=3, col=1)
        fig_main.add_hline(y=0.03, line_color='#ff4b4b', line_width=0.8,
                           line_dash='dot', annotation_text="過熱 0.03%", row=3, col=1)

        # Row 4: TVL
        if not tvl_hist.empty:
            _th = tvl_hist.copy()
            if _th.index.tz is not None:
                _th.index = _th.index.tz_localize(None)
            tvl_sub = _th.reindex(_cdf.index, method='nearest')
            fig_main.add_trace(go.Scatter(
                x=tvl_sub.index, y=tvl_sub['tvl'] if 'tvl' in tvl_sub.columns else [],
                mode='lines', fill='tozeroy', line=dict(color='#a32eff'), name='TVL (USD)',
            ), row=4, col=1)

        # Row 5: 穩定幣市值
        if not stable_hist.empty:
            stab_sub = stable_hist.reindex(_cdf.index, method='nearest')
            fig_main.add_trace(go.Scatter(
                x=stab_sub.index, y=stab_sub['mcap'] / 1e9,
                mode='lines', line=dict(color='#2E86C1'), name='Stablecoin Cap ($B)',
            ), row=5, col=1)

        fig_main.update_layout(
            height=1000, template="plotly_dark", xaxis_rangeslider_visible=False,
            legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='right', x=1),
        )
        st.session_state[ss_main_key] = fig_main
        st.session_state[ss_hash_key] = cache_key

    st.plotly_chart(fig_main, use_container_width=True)
    st.markdown("---")

    # ══════════════════════════════════════════════════════════════
    # Section 2: 指標評分明細（卡片化 Level 1-3）
    # ══════════════════════════════════════════════════════════════
    st.subheader("B. 多空指標評分明細 (Level 1 ~ Level 3)")

    # ── Level 1: 散戶視角 ────────────────────────────────────────
    st.markdown("#### Level 1 · 散戶視角 (Price & Sentiment)")
    is_golden  = (curr['close'] > ma200) and (ma50 > ma200)
    is_rising  = ma200_slope > 0
    struct_state = ("多頭共振 (STRONG)" if (is_golden and is_rising)
                    else ("震盪/修正 (WEAK)" if not is_golden else "年線走平 (FLAT)"))
    recent_high  = btc['high'].iloc[-20:].max()
    prev_high    = btc['high'].iloc[-40:-20].max()
    dow_state    = "更高的高點 (HH)" if recent_high > prev_high else "高點降低 (LH)"

    l1_cols = st.columns(3)
    l1_data = [
        ("趨勢結構",    struct_state,  f"MA200 斜率 {'↗️ 上升' if is_rising else '↘️ 下降'}", "本地計算 (SMA200 斜率)"),
        ("道氏理論",    dow_state,     "近 20 日 vs 前 20 日高點",                            "本地計算 (高低點比較)"),
        (f"情緒指數",   f"{fng_val:.0f}/100", fng_state,                                     fng_source),
    ]
    for col, (title, val, delta, src) in zip(l1_cols, l1_data):
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{title}</div>
            <div class="metric-value">{val}</div>
            <div class="metric-delta">{delta}</div>
            <div class="metric-source">來源：{src}</div>
        </div>""", unsafe_allow_html=True)

    # ── Level 2: 機構視角 ────────────────────────────────────────
    st.markdown("#### Level 2 · 機構視角 (On-Chain & Derivatives)")
    ahr_val  = curr.get('AHR999', float('nan'))
    mvrv_z   = curr.get('MVRV_Z_Proxy', 0) or 0
    etf_flow = proxies['etf_flow']
    fr_state = ("🔥 多頭過熱" if funding_rate > 0.03
                else ("🟢 情緒中性" if funding_rate > 0 else "❄️ 空頭主導"))

    ahr_state = ("🟢 抄底區間" if ahr_val < 0.45 else ("🟡 合理區間" if ahr_val < 1.2 else "🔴 高估區間"))
    mvrv_state = ("🔥 過熱頂部" if mvrv_z > 3.0 else ("🟢 價值低估" if mvrv_z < 0 else "中性區域"))

    _tvl_source = realtime_data.get('tvl_source') or 'DeFiLlama'
    _fr_source  = realtime_data.get('funding_rate_source') or '模擬值'
    l2_cols = st.columns(5)
    l2_data = [
        ("AHR999 囤幣指標", f"{ahr_val:.3f}",                    ahr_state,                                    "本地計算 (Price/SMA200 × Price/PowerLaw)"),
        ("MVRV Z-Score",    f"{mvrv_z:.2f}",                     mvrv_state,                                   "本地計算 (Price-SMA200)/σ200"),
        ("BTC 生態 TVL",    f"${tvl_val/1e9:.2f}B" if tvl_val>1e9 else f"${tvl_val:.2f}B",
                                                                  "↑ 持續增長" if tvl_val>0 else "↓ 資金流出", _tvl_source),
        ("ETF 淨流量(24h)", f"{etf_flow:+.1f}M",                 "↑ 機構買盤" if etf_flow>0 else "↓ 機構拋壓", "模擬估算 (價格變化 Proxy)"),
        ("資金費率",        f"{funding_rate:.4f}%",               fr_state,                                    _fr_source),
    ]
    for col, (title, val, delta, src) in zip(l2_cols, l2_data):
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{title}</div>
            <div class="metric-value">{val}</div>
            <div class="metric-delta">{delta}</div>
            <div class="metric-source">來源：{src}</div>
        </div>""", unsafe_allow_html=True)

    # ── Level 3: 宏觀視角 ────────────────────────────────────────
    st.markdown("#### Level 3 · 宏觀視角 (Macro)")
    m3_col1, m3_col2, m3_col3, m3_col4 = st.columns(4)

    # DXY 相關性
    with m3_col1:
        dxy_is_fb = getattr(dxy, 'is_fallback', False)
        if not dxy.empty and not dxy_is_fb:
            _btc2 = btc.copy()
            _dxy2 = dxy.copy()
            if _btc2.index.tz is not None: _btc2.index = _btc2.index.tz_localize(None)
            if _dxy2.index.tz is not None: _dxy2.index = _dxy2.index.tz_localize(None)
            comm = _btc2.index.intersection(_dxy2.index)
            if len(comm) >= 90:
                corr = _btc2.loc[comm]['close'].rolling(90).corr(_dxy2.loc[comm]['close']).iloc[-1]
                corr_val = f"{corr:.2f}" if corr == corr else "—"
                st.metric("BTC vs DXY 90d 相關係數", corr_val, "負相關 (正常)" if corr == corr and corr < -0.5 else "相關性減弱")
                st.caption("來源：本地計算 (Yahoo Finance DXY)")
            else:
                st.metric("BTC vs DXY 90d", "—", "數據不足")
        else:
            st.metric("BTC vs DXY 90d", "—", "DXY 數據暫不可用")

    # M2
    with m3_col2:
        m2_df = fetch_m2_series()
        if not m2_df.empty:
            m2_val    = m2_df['m2_billions'].iloc[-1]
            m2_is_fb  = getattr(m2_df, 'is_fallback', False)
            m2_src    = f"備援值 ({_FALLBACK['m2']['date']})" if m2_is_fb else "FRED WM2NS"
            st.metric("美國 M2", f"${m2_val:,.0f}B", "貨幣供應量")
            st.caption(f"來源：{m2_src}")
        else:
            st.metric("美國 M2", "—", "數據暫不可用")

    # JPY
    with m3_col3:
        jpy = fetch_usdjpy()
        if jpy.get('rate') is not None:
            jpy_src = jpy.get('source', '備援值')
            st.metric("🇯🇵 USD/JPY", f"¥{jpy['rate']:.2f}",
                      f"{jpy['change_pct']:+.2f}% {jpy['trend']}")
            st.caption(f"來源：{jpy_src}")
        else:
            st.metric("🇯🇵 USD/JPY", "—", "數據暫不可用")

    # CPI
    with m3_col4:
        cpi = fetch_us_cpi_yoy()
        if cpi.get('yoy_pct') is not None:
            cpi_src = cpi.get('source', '備援值')
            st.metric(f"🇺🇸 CPI YoY ({cpi['latest_date']})",
                      f"{cpi['yoy_pct']:.1f}%", cpi['trend'])
            st.caption(f"來源：{cpi_src}")
        else:
            st.metric("🇺🇸 CPI YoY", "—", "數據暫不可用")

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════
    # Section 3: 熊市底部獵人 (Bear Bottom Hunter)
    # ══════════════════════════════════════════════════════════════
    st.subheader("C. 熊市底部獵人 (Bear Bottom Hunter)")
    st.caption("整合 8 大鏈上+技術指標，量化評估當前是否接近歷史性熊市底部")

    curr_score, curr_signals = calculate_bear_bottom_score(btc.iloc[-1])
    score_level, score_color, score_action = _bear_score_meta(curr_score)

    # 底部評分 Gauge
    fig_bb_gauge = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=curr_score,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={
            'text': "熊市底部評分<br><span style='font-size:0.8em;color:gray'>Bear Bottom Score</span>",
            'font': {'size': 18},
        },
        delta={'reference': 50, 'increasing': {'color': '#ff4b4b'}, 'decreasing': {'color': '#00ff88'}},
        gauge={
            'axis': {'range': [0, 100], 'tickwidth': 1, 'tickcolor': 'white'},
            'bar': {'color': score_color},
            'bgcolor': '#1e1e1e',
            'borderwidth': 2, 'bordercolor': '#333',
            'steps': [
                {'range': [0, 25],   'color': '#1a3a1a'},
                {'range': [25, 45],  'color': '#2a2a2a'},
                {'range': [45, 60],  'color': '#3a3a1a'},
                {'range': [60, 75],  'color': '#3a2a1a'},
                {'range': [75, 100], 'color': '#3a1a1a'},
            ],
            'threshold': {'line': {'color': '#ffffff', 'width': 3}, 'thickness': 0.75, 'value': curr_score},
        },
    ))
    fig_bb_gauge.update_layout(
        height=280, template="plotly_dark",
        paper_bgcolor="#0e1117", font={'color': 'white'},
    )

    bg_c1, bg_c2 = st.columns([1, 1])
    with bg_c1:
        st.plotly_chart(fig_bb_gauge, use_container_width=True)
    with bg_c2:
        st.markdown(f"### {score_level}")
        st.markdown(f"**評分: {curr_score}/100**")
        st.info(f"📋 **操作建議**: {score_action}")
        st.markdown("""
        | 分數區間 | 市場狀態 | 建議行動 |
        |---------|---------|---------|
        | 75-100  | 歷史極值底部 | 全力積累 |
        | 60-75   | 明確底部區間 | 重倉布局 |
        | 45-60   | 可能底部區  | 分批試探 |
        | 25-45   | 震盪修正    | 觀望等待 |
        | 0-25    | 牛市高估    | 持有/減倉 |
        """)
    st.markdown("---")

    # 八大指標卡片（來源全部為本地計算）
    st.subheader("C1. 八大指標評分明細")
    st.caption("所有指標均由本地歷史 K 線計算，無需外部 API")
    indicator_cols = st.columns(4)
    for idx, (key, sig) in enumerate(curr_signals.items()):
        col = indicator_cols[idx % 4]
        bar_pct = sig['score'] / sig['max'] * 100
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{key.replace('_', ' ')}</div>
            <div class="metric-value">{sig['value']}</div>
            <div class="metric-delta">{sig['label']}</div>
            <div style="background:#333;border-radius:4px;height:6px;margin-top:8px;">
                <div style="background:{score_color};width:{bar_pct:.0f}%;height:6px;border-radius:4px;"></div>
            </div>
            <div style="color:#888;font-size:0.75rem;text-align:right;">{sig['score']}/{sig['max']} 分</div>
            <div class="metric-source">來源：本地計算</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")

    # 歷史底部驗證圖（含快取）
    st.subheader("C2. 歷史熊市底部驗證 (Bear Market Bottoms Map)")
    st.caption("橙色區域 = 已知熊市底部 | 藍線 = 200週均線 | 紅線 = Pi Cycle | 黃線 = 冪律支撐 | 青線 = SMA50")

    bb_cache_key = _make_bb_cache_key(btc)
    ss_hist_key  = f"tab_mc_fig_hist_{bb_cache_key}"

    if st.session_state.get("tab_mc_bb_key") == bb_cache_key and ss_hist_key in st.session_state:
        fig_hist = st.session_state[ss_hist_key]
    else:
        fig_hist = make_subplots(
            rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
            row_heights=[0.5, 0.25, 0.25],
            subplot_titles=(
                "BTC 價格 + 底部指標均線 (對數坐標)",
                "Pi Cycle Gap (SMA111 vs 2×SMA350) — 負值觸底信號",
                "Puell Multiple Proxy — <0.5 礦工投降底部",
            ),
        )
        fig_hist.add_trace(go.Scatter(
            x=btc.index, y=btc['close'], mode='lines', name='BTC 價格',
            line=dict(color='#ffffff', width=1.5),
        ), row=1, col=1)
        if 'SMA_1400' in btc.columns and btc['SMA_1400'].notna().any():
            fig_hist.add_trace(go.Scatter(
                x=btc.index, y=btc['SMA_1400'], mode='lines', name='200週均線',
                line=dict(color='#2196F3', width=2),
            ), row=1, col=1)
        if 'SMA_350x2' in btc.columns and btc['SMA_350x2'].notna().any():
            fig_hist.add_trace(go.Scatter(
                x=btc.index, y=btc['SMA_350x2'], mode='lines', name='2×SMA350 (Pi Cycle上軌)',
                line=dict(color='#ff4b4b', width=1.5, dash='dash'),
            ), row=1, col=1)
        if 'SMA_111' in btc.columns and btc['SMA_111'].notna().any():
            fig_hist.add_trace(go.Scatter(
                x=btc.index, y=btc['SMA_111'], mode='lines', name='SMA111',
                line=dict(color='#ff8800', width=1.5),
            ), row=1, col=1)
        if 'PowerLaw_Support' in btc.columns and btc['PowerLaw_Support'].notna().any():
            fig_hist.add_trace(go.Scatter(
                x=btc.index, y=btc['PowerLaw_Support'], mode='lines', name='冪律支撐線',
                line=dict(color='#ffcc00', width=1.5, dash='dot'),
            ), row=1, col=1)
        for b_start, b_end, b_label in KNOWN_BOTTOMS:
            try:
                fig_hist.add_vrect(
                    x0=b_start, x1=b_end,
                    fillcolor="rgba(255,140,0,0.15)", layer="below", line_width=0,
                    annotation_text=b_label, annotation_position="top left",
                    row=1, col=1,
                )
            except Exception:
                pass
        if 'PiCycle_Gap' in btc.columns and btc['PiCycle_Gap'].notna().any():
            pi_c = ['#ff4b4b' if v > 0 else '#00ff88' for v in btc['PiCycle_Gap'].fillna(0)]
            fig_hist.add_trace(go.Bar(
                x=btc.index, y=btc['PiCycle_Gap'], marker_color=pi_c, name='Pi Cycle Gap (%)', showlegend=False,
            ), row=2, col=1)
            fig_hist.add_hline(y=0,  line_color='white',   line_width=1,   opacity=0.5, row=2, col=1)
            fig_hist.add_hline(y=-5, line_color='#00ff88', line_width=1,   line_dash='dash',
                               annotation_text="底部信號線", row=2, col=1)
        if 'Puell_Proxy' in btc.columns and btc['Puell_Proxy'].notna().any():
            fig_hist.add_trace(go.Scatter(
                x=btc.index, y=btc['Puell_Proxy'], mode='lines',
                line=dict(color='#a32eff', width=1.5), name='Puell Proxy', showlegend=False,
            ), row=3, col=1)
            fig_hist.add_hline(y=0.5, line_color='#00ff88', line_width=1.5, line_dash='dash',
                               annotation_text="0.5 底部線", row=3, col=1)
            fig_hist.add_hline(y=4.0, line_color='#ff4b4b', line_width=1.5, line_dash='dash',
                               annotation_text="4.0 頂部線", row=3, col=1)
        fig_hist.update_layout(
            height=850, template="plotly_dark", xaxis_rangeslider_visible=False,
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        )
        fig_hist.update_yaxes(type="log", row=1, col=1)
        st.session_state[ss_hist_key]    = fig_hist
        st.session_state["tab_mc_bb_key"] = bb_cache_key

    st.plotly_chart(fig_hist, use_container_width=True)
    st.markdown("---")

    # 歷史評分走勢
    st.subheader("C3. 歷史底部評分走勢 (Bottom Score History)")
    ss_score_key = f"tab_mc_fig_score_{bb_cache_key}"

    if st.session_state.get("tab_mc_bb_key") == bb_cache_key and ss_score_key in st.session_state:
        fig_score = st.session_state[ss_score_key]
    else:
        score_slice = btc.tail(365*4).copy()
        with st.spinner("正在計算歷史底部評分..."):
            score_slice['BottomScore'] = score_series(score_slice)

        fig_score = make_subplots(
            rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.05,
            row_heights=[0.4, 0.6],
            subplot_titles=("底部評分 (0-100)", "BTC 價格 (對數)"),
        )
        sc_colors = ['#ff4b4b' if s < 25 else ('#ffcc00' if s < 45 else ('#ff8800' if s < 60 else '#00ccff'))
                     for s in score_slice['BottomScore']]
        fig_score.add_trace(go.Bar(
            x=score_slice.index, y=score_slice['BottomScore'],
            marker_color=sc_colors, name='底部評分', showlegend=False,
        ), row=1, col=1)
        fig_score.add_hline(y=60, line_color='#00ccff', line_dash='dash', annotation_text="60分 積極積累線", row=1, col=1)
        fig_score.add_hline(y=45, line_color='#ffcc00', line_dash='dot',  annotation_text="45分 試探線",    row=1, col=1)
        fig_score.add_trace(go.Scatter(
            x=score_slice.index, y=score_slice['close'],
            mode='lines', name='BTC 價格', line=dict(color='#ffffff', width=1.5),
        ), row=2, col=1)
        high_score = score_slice[score_slice['BottomScore'] >= 60]
        if not high_score.empty:
            fig_score.add_trace(go.Scatter(
                x=high_score.index, y=high_score['close'], mode='markers',
                name='底部積累區 (≥60分)',
                marker=dict(color='#00ccff', size=5, symbol='circle', opacity=0.7),
            ), row=2, col=1)
        fig_score.update_yaxes(type="log", row=2, col=1)
        fig_score.update_layout(
            height=600, template="plotly_dark",
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        )
        st.session_state[ss_score_key] = fig_score

    st.plotly_chart(fig_score, use_container_width=True)

    # 指標一覽表
    st.markdown("---")
    st.subheader("C4. 當前關鍵底部指標一覽")
    curr_row = btc.iloc[-1]
    summary_data = {
        "指標": ["AHR999 囤幣指標", "MVRV Z-Score (Proxy)", "Pi Cycle Gap",
                  "200週均線比值", "Puell Multiple (Proxy)", "月線 RSI", "冪律支撐倍數", "Mayer Multiple"],
        "當前值": [
            f"{curr_row.get('AHR999', float('nan')):.3f}",
            f"{curr_row.get('MVRV_Z_Proxy', float('nan')):.2f}",
            f"{curr_row.get('PiCycle_Gap', float('nan')):.1f}%",
            f"{curr_row.get('SMA200W_Ratio', float('nan')):.2f}x",
            f"{curr_row.get('Puell_Proxy', float('nan')):.2f}",
            f"{curr_row.get('RSI_Monthly', float('nan')):.1f}",
            f"{curr_row.get('PowerLaw_Ratio', float('nan')):.1f}x",
            f"{curr_row.get('Mayer_Multiple', float('nan')):.2f}x",
        ],
        "底部閾值": ["< 0.45", "< 0", "< -5%", "< 1.0x", "< 0.5", "< 30", "< 2x", "< 0.8x"],
        "頂部閾值": ["> 1.2", "> 3.5", "> 10%", "> 4x", "> 4.0", "> 75", "> 10x", "> 2.4x"],
    }
    st.dataframe(pd.DataFrame(summary_data), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ══════════════════════════════════════════════════════════════
    # Section 4: 四季理論目標價預測 (來自 Tab 5 Section F)
    # ══════════════════════════════════════════════════════════════
    st.subheader("D. 🗓️ 四季理論目標價預測 (Halving Cycle Forecast)")
    st.caption(
        "依比特幣減半週期（約4年）劃分四季，整合歷史漲跌倍數與冪律模型，"
        "預測未來12個月牛市最高價或熊市最低價。"
    )

    current_price = float(btc.iloc[-1]["close"])
    fc = forecast_price(current_price, df=btc)

    if fc is None:
        st.error("無法取得減半週期資訊，請確認數據範圍。")
    else:
        si          = fc["season_info"]
        eff         = fc["effective_season"]
        ms          = fc["market_state"]
        is_bull     = fc["forecast_type"] == "bull_peak"
        is_corrected = fc.get("is_season_corrected", False)
        eff_color   = _season_css_color(eff["season"])
        time_color  = _season_css_color(si["season"])

        # F1. 季節狀態橫幅
        drawdown_pct = abs(ms["drawdown_from_ath"]) * 100
        sma200_val   = ms["sma200"]
        above_str    = "✅ 站上" if ms["is_above_sma200"] else "❌ 跌破"

        if is_corrected:
            season_header = f"""
            <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
                <div style="opacity:0.45;text-decoration:line-through;font-size:1.1rem;color:{time_color};">
                    {si['emoji']} {si['season_zh']} (時間)
                </div>
                <div style="font-size:1.3rem;color:#888;">→</div>
                <div style="font-size:2rem;font-weight:800;color:{eff_color};">
                    {eff['emoji']} {eff['season_zh']} (市場實際)
                </div>
            </div>"""
        else:
            season_header = f"""
            <div style="font-size:2rem;font-weight:700;color:{eff_color};">
                {eff['emoji']} {eff['season_zh']}
            </div>"""

        st.markdown(
            f"""
            <div style="
                background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                border: 2px solid {eff_color};
                border-radius: 12px;
                padding: 20px 28px;
                margin-bottom: 16px;
            ">
                {season_header}
                <div style="color:#ccc; margin-top:10px; font-size:0.95rem;">
                    第 <b style="color:white">{fc['current_cycle_idx']+1}</b> 次減半週期
                    &nbsp;｜&nbsp;
                    減半日: <b style="color:white">{si['halving_date'].strftime('%Y-%m-%d')}</b>
                    &nbsp;｜&nbsp;
                    已過 <b style="color:white">{si['days_since']}</b> 天 /
                    距下次減半還有 <b style="color:white">{si['days_to_next']}</b> 天
                </div>
                <div style="color:#aaa; margin-top:6px; font-size:0.88rem; display:flex; gap:24px; flex-wrap:wrap;">
                    <span>週期月份: <b style="color:white">第 {si['month_in_cycle']} 個月</b></span>
                    <span>週期進度: <b style="color:white">{si['cycle_progress']*100:.1f}%</b></span>
                    <span>距ATH跌幅: <b style="color:{'#ff6b6b' if drawdown_pct > 15 else '#ffd93d'}">
                        -{drawdown_pct:.1f}%</b> (ATH ${ms['cycle_ath']:,.0f})</span>
                    <span>200日均線: <b style="color:white">{above_str} ${sma200_val:,.0f}</b></span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if is_corrected and fc.get("correction_reason"):
            st.warning(fc["correction_reason"])

        st.plotly_chart(_render_season_timeline(si, effective_season=eff["season"]), use_container_width=True)
        st.markdown("---")

        # F2. 目標價卡片
        fc_type_zh   = "📈 牛市最高價預測" if is_bull else "📉 熊市最低價預測"
        target_color = "#ffeb3b" if is_bull else "#42a5f5"
        conf_bar     = fc["confidence"]
        lbl_low      = fc.get("bear_label_low",  "25th 百分位")
        lbl_high     = fc.get("bear_label_high", "75th 百分位")
        ath_ref_hint = ""
        if not is_bull and fc.get("ath_ref"):
            ath_ref_hint = f"<div style='color:#666;font-size:0.7rem;margin-top:2px;'>基準ATH: ${fc['ath_ref']:,.0f}</div>"

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            side_title = "最深目標 ↓" if not is_bull else "保守目標 ↑"
            st.markdown(
                f"""<div style="background:#1e2a1e;border:1px solid {target_color};border-radius:10px;padding:18px;text-align:center;">
                    <div style="color:#888;font-size:0.8rem;">{side_title}</div>
                    <div style="color:{target_color};font-size:1.6rem;font-weight:700;">${fc['target_low']:,.0f}</div>
                    <div style="color:#666;font-size:0.75rem;">{lbl_low}</div>
                    {ath_ref_hint}</div>""", unsafe_allow_html=True,
            )
        with col_b:
            st.markdown(
                f"""<div style="background:#1e2a1e;border:2px solid {target_color};border-radius:10px;padding:18px;text-align:center;box-shadow:0 0 12px {target_color}44;">
                    <div style="color:#aaa;font-size:0.85rem;">{fc_type_zh}</div>
                    <div style="color:{target_color};font-size:2.2rem;font-weight:800;">${fc['target_median']:,.0f}</div>
                    <div style="color:#999;font-size:0.8rem;">歷史中位數目標</div>
                    <div style="color:#666;font-size:0.75rem;margin-top:4px;">預計達標: {fc['estimated_date'].strftime('%Y-%m-%d')}</div>
                    {ath_ref_hint}</div>""", unsafe_allow_html=True,
            )
        with col_c:
            side_title = "最淺目標 ↑" if not is_bull else "樂觀目標 ↑"
            st.markdown(
                f"""<div style="background:#1e2a1e;border:1px solid {target_color};border-radius:10px;padding:18px;text-align:center;">
                    <div style="color:#888;font-size:0.8rem;">{side_title}</div>
                    <div style="color:{target_color};font-size:1.6rem;font-weight:700;">${fc['target_high']:,.0f}</div>
                    <div style="color:#666;font-size:0.75rem;">{lbl_high}</div>
                    {ath_ref_hint}</div>""", unsafe_allow_html=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        conf_color = "#00e676" if conf_bar >= 65 else ("#ffeb3b" if conf_bar >= 45 else "#ff9800")
        st.markdown(
            f"""<div style="margin:8px 0 16px 0;">
                <div style="color:#aaa;font-size:0.85rem;margin-bottom:4px;">
                    預測信心分數: <b style="color:{conf_color};">{conf_bar}/100</b>
                </div>
                <div style="background:#333;border-radius:6px;height:10px;">
                    <div style="background:{conf_color};width:{conf_bar}%;height:10px;border-radius:6px;"></div>
                </div></div>""",
            unsafe_allow_html=True,
        )

        with st.expander("📖 預測邏輯說明", expanded=False):
            st.info(fc["rationale"])

        # F3. 預測走勢圖（含快取）
        st.markdown("#### D3. 目標價走勢圖（過去2年 + 未來12個月）")
        ss_fc_key = f"tab_mc_fig_fc_{bb_cache_key}"
        if st.session_state.get("tab_mc_bb_key") == bb_cache_key and ss_fc_key in st.session_state:
            fig_fc = st.session_state[ss_fc_key]
        else:
            with st.spinner("建立預測走勢圖..."):
                fig_fc = _render_forecast_chart(btc, fc)
            st.session_state[ss_fc_key] = fig_fc
        st.plotly_chart(fig_fc, use_container_width=True)

        # F4. 歷史週期比較表 + 瀑布圖
        st.markdown("---")
        st.markdown("#### D4. 歷史減半週期比較")
        st.caption("✅ = 完整週期 ｜ 🔄 = 進行中")
        col_tbl, col_bar = st.columns([1.3, 1])
        with col_tbl:
            st.dataframe(get_cycle_comparison_table(), use_container_width=True, hide_index=True)
        with col_bar:
            st.plotly_chart(_render_cycle_waterfall(fc), use_container_width=True)

        # F5. 四季操作策略
        st.markdown("---")
        st.markdown("#### D5. 四季操作策略")
        strat_cols = st.columns(4)
        strategies = [
            ("🌱", "春季 (月0-11)", "#1b5e20",
             "減半後復甦期。市場情緒由恐懼轉向觀望，適合**分批建倉**，重點佈局主流幣。"),
            ("☀️", "夏季 (月12-23)", "#f57f17",
             "牛市加速期。FOMO情緒蔓延，適合**持有並設置移動止盈**，避免頂部加倉。"),
            ("🍂", "秋季 (月24-35)", "#e65100",
             "泡沫破裂期。高點已過，空頭確立，適合**逐步減倉**，轉向穩定資產。"),
            ("❄️", "冬季 (月36-47)", "#0d47a1",
             "熊市底部期。恐慌拋售為主，適合**定期定額囤幣**，等待下一個春天。"),
        ]
        for col, (emoji, name, bg, desc) in zip(strat_cols, strategies):
            is_current = name.startswith(eff["emoji"]) or name.startswith(si["emoji"])
            border   = f"2px solid {eff_color}" if is_current else "1px solid #333"
            cur_tag  = (f"<div style='color:{eff_color};font-size:0.8rem;margin-top:8px;font-weight:600;'>← 當前季節</div>"
                        if is_current else "")
            col.markdown(
                f"""<div style="background:{bg}22;border:{border};border-radius:10px;padding:14px;min-height:160px;">
                    <div style="font-size:1.6rem;">{emoji}</div>
                    <div style="color:white;font-weight:600;margin:4px 0;">{name}</div>
                    <div style="color:#ccc;font-size:0.82rem;">{desc}</div>
                    {cur_tag}
                </div>""",
                unsafe_allow_html=True,
            )

    st.markdown("""
    ---
    > **免責聲明**: 以上指標均為技術分析工具，不構成投資建議。
    > 歷史數據不代表未來表現。加密貨幣市場波動劇烈，請嚴格控制倉位風險。
    > Pi Cycle 冪律模型參數來源: Giovanni Santostasi 比特幣冪律理論。
    > 四季理論基於歷史減半週期規律，每個週期漲幅遞減為已知趨勢，實際結果可能顯著偏離。
    """)
