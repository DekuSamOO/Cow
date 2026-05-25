"""
Backtest Sub-Tab 3：牛市雷達準確度驗證

從 handler/tab_backtest.py 拆分。同時繪製 MA200 + MA50，與驗證邏輯一致。
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import numpy as np


def render(btc, ahr_threshold=None):
    """Sub-Tab 3: 牛市雷達準確度"""
    st.markdown("#### 🐂 牛市雷達準確度驗證")
    st.caption(
        "驗證：黃金交叉 (Close > MA200 & **MA50 > MA200**) + 年線上揚 (MA200 Slope > 0)\n"
        "圖表同時繪製 **MA200（橙色）** 與 **MA50（青色）**，讓金叉/死叉視覺與文字條件完全對應。"
    )

    _ahr_threshold = st.slider(
        "AHR999 抄底閾值",
        min_value=0.3, max_value=1.5,
        value=float(ahr_threshold) if ahr_threshold is not None else 0.45,
        step=0.05,
        help="AHR999 低於此值時標記為抄底買入信號（圖表中青色散點）",
    )

    bull_ranges = [
        ("2017-01", "2017-12"),
        ("2020-10", "2021-04"),
        ("2023-10", "2024-03"),
        ("2024-10", "2025-01"),
    ]

    val_df = btc.copy()
    sma200_valid = val_df['SMA_200'].notna()
    sma50_valid  = val_df['SMA_50'].notna()
    slope_valid  = val_df['SMA_200_Slope'].notna()

    val_df['Trend_Bull'] = (
        sma200_valid & sma50_valid & slope_valid &
        (val_df['close'] > val_df['SMA_200'].fillna(0)) &
        (val_df['SMA_50'] > val_df['SMA_200'].fillna(0)) &
        (val_df['SMA_200_Slope'].fillna(0) > 0)
    )
    val_df['Signal_Bull'] = val_df['Trend_Bull']
    val_df['Actual_Bull'] = False

    for start, end in bull_ranges:
        try:
            s_dt = pd.to_datetime(start)
            e_dt = pd.to_datetime(end) + pd.offsets.MonthEnd(0)
            val_df.loc[s_dt:e_dt, 'Actual_Bull'] = True
        except Exception:
            pass

    conditions = [
        (val_df['Signal_Bull']) & (val_df['Actual_Bull']),
        (val_df['Signal_Bull']) & (~val_df['Actual_Bull']),
        (~val_df['Signal_Bull']) & (val_df['Actual_Bull']),
        (~val_df['Signal_Bull']) & (~val_df['Actual_Bull']),
    ]
    choices = ['Correct Bull', 'False Alarm (Trap)', 'Missed Opportunity', 'Correct Bear']
    val_df['Result'] = np.select(conditions, choices, default='Unknown')

    total_days  = len(val_df)
    counts      = val_df['Result'].value_counts()
    c_bull      = counts.get('Correct Bull', 0)
    c_trap      = counts.get('False Alarm (Trap)', 0)
    c_miss      = counts.get('Missed Opportunity', 0)
    bull_days   = len(val_df[val_df['Actual_Bull']])
    sensitivity = c_bull / bull_days * 100 if bull_days > 0 else 0
    acc_total   = (c_bull + counts.get('Correct Bear', 0)) / total_days * 100

    v1, v2, v3, v4 = st.columns(4)
    v1.metric("牛市捕捉率", f"{sensitivity:.1f}%", f"{c_bull} 天命中")
    v2.metric("誤報天數", f"{c_trap} 天", delta_color="inverse")
    v3.metric("踏空天數", f"{c_miss} 天", delta_color="inverse")
    v4.metric("整體準確度", f"{acc_total:.1f}%")

    val_df['AHR_Signal'] = val_df['AHR999'] < _ahr_threshold

    fig_m = go.Figure()
    fig_m.add_trace(go.Scatter(
        x=val_df.index, y=val_df['close'],
        mode='lines', name='Price', line=dict(color='gray', width=1),
    ))
    fig_m.add_trace(go.Scatter(
        x=val_df.index, y=val_df['SMA_200'],
        mode='lines', name='SMA 200',
        line=dict(color='orange', width=1.5),
    ))
    fig_m.add_trace(go.Scatter(
        x=val_df.index, y=val_df['SMA_50'],
        mode='lines', name='SMA 50',
        line=dict(color='cyan', width=1.2, dash='dash'),
    ))

    traps = val_df[val_df['Result'] == 'False Alarm (Trap)']
    if not traps.empty:
        fig_m.add_trace(go.Scatter(
            x=traps.index, y=traps['close'], mode='markers',
            name='❌ 誤判', marker=dict(color='#ff4b4b', size=8, symbol='x'),
        ))
    corrects = val_df[val_df['Result'] == 'Correct Bull']
    if not corrects.empty:
        fig_m.add_trace(go.Scatter(
            x=corrects.index, y=corrects['close'], mode='markers',
            name='✅ 命中', marker=dict(color='#00ff88', size=4, opacity=0.4),
        ))
    ahr_buys = val_df[val_df['AHR_Signal']]
    if not ahr_buys.empty:
        fig_m.add_trace(go.Scatter(
            x=ahr_buys.index, y=ahr_buys['close'] * 0.9, mode='markers',
            name=f'AHR < {_ahr_threshold} (Buy Zone)',
            marker=dict(color='cyan', size=2, opacity=0.3),
        ))

    fig_m.update_layout(
        title="策略有效性驗證（橙色=MA200，青色=MA50，金叉區間=訊號觸發）",
        height=400, template="plotly_dark", yaxis_type="log",
    )
    st.plotly_chart(fig_m, use_container_width=True)

    with st.expander("📖 驗證條件說明"):
        st.markdown("""
        **買入訊號觸發條件（三合一）**:
        1. `Close > SMA_200` — 價格站上 200 日均線（多頭市場確認）
        2. `SMA_50 > SMA_200` — 金叉：50 日均線穿越 200 日均線上方（圖表橙線 vs 青線）
        3. `SMA_200 Slope > 0` — 200 日均線斜率為正（年線趨勢向上）

        圖表中橙色為 SMA200、青色為 SMA50，
        當青色（SMA50）在橙色（SMA200）上方時即為金叉狀態，與文字條件完全對應。
        """)
