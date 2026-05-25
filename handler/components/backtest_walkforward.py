"""
Backtest Sub-Tab 5：Walk-Forward 無先視回測

從 handler/tab_backtest.py 拆分。逐日推進、防先視偏誤的回測引擎。
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from datetime import timedelta

from strategy.walkforward_backtest import WalkForwardBacktester
from config import (
    DEFAULT_INITIAL_CAPITAL,
    WALK_FORWARD_EXIT_MODES,
    DEFAULT_WALK_FORWARD_EXIT_MODE,
)
from handler.components._utils import df_to_csv_bytes


def render(btc):
    """Sub-Tab 5: Walk-Forward 無先視回測"""
    st.markdown("#### 🚀 Walk-Forward 無先視回測（逐日推進）")
    st.caption(
        "🛡️ **無先視偏誤設計**：每日掃描的進場條件只基於該日及以前的資料，"
        "模擬實際交易的『只知已知資訊』真實情境。"
    )

    wf_col1, wf_col2 = st.columns([1, 3])

    with wf_col1:
        st.subheader("⚙️ 回測設定")
        wf_min_date = btc.index[0].date()
        wf_max_date = btc.index[-1].date()
        wf_start = st.date_input(
            "開始日期", value=wf_min_date + timedelta(days=365),
            min_value=wf_min_date, max_value=wf_max_date, key="wf_start",
        )
        wf_end = st.date_input(
            "結束日期", value=wf_max_date,
            min_value=wf_min_date, max_value=wf_max_date, key="wf_end",
        )
        wf_init_cap = st.number_input(
            "初始本金 (USDT)",
            value=int(DEFAULT_INITIAL_CAPITAL),
            step=1_000, key="wf_cap",
        )

        st.markdown("---")
        st.markdown("**進場條件（Antigravity v4.1 五合一）**")
        wf_dist = st.slider(
            "EMA20 最小乖離 (%)",
            min_value=0.0, max_value=2.0, value=0.0, step=0.1,
            help="0 = 只要站上 EMA20 即符合", key="wf_dist",
        )
        _wf_dist_max_raw = st.slider(
            "EMA20 最大乖離 (%) ── 0 = 不限",
            min_value=0.0, max_value=20.0, value=0.0, step=0.5,
            help="0 = 不設上限（等同 swing.py 行為）；設值後只在乖離 ≤ 此值時才進場",
            key="wf_dist_max",
        )
        wf_dist_max = None if _wf_dist_max_raw == 0.0 else _wf_dist_max_raw
        wf_rsi = st.slider(
            "RSI 動能閾值",
            min_value=40, max_value=65, value=50, step=1, key="wf_rsi",
        )
        wf_adx = st.slider(
            "ADX 趨勢強度閾值",
            min_value=10, max_value=35, value=20, step=1, key="wf_adx",
        )
        wf_exit_ma = st.selectbox(
            "波段防守線 (出場條件)",
            options=["SMA_50", "EMA_20", "SMA_200"],
            index=0, key="wf_exit_ma",
        )

        st.markdown("---")
        st.markdown("**出場策略模式**")
        _exit_mode_keys = list(WALK_FORWARD_EXIT_MODES.keys())
        wf_exit_mode = st.radio(
            "選擇出場機制",
            options=_exit_mode_keys,
            format_func=lambda x: WALK_FORWARD_EXIT_MODES.get(x, x),
            horizontal=True,
            key="wf_exit_mode",
            index=_exit_mode_keys.index(DEFAULT_WALK_FORWARD_EXIT_MODE),
        )
        st.caption(
            "💡 簡化模式適合長期持倉，進階模式包含極端出貨、ATR目標、時間停損等多層保護。"
        )

        st.markdown("**出場參數**")
        wf_atr_sl = st.slider(
            "ATR 停損倍數",
            min_value=0.5, max_value=3.0, value=2.0, step=0.25, key="wf_atr_sl",
            help="停損線 = 進場價 - ATR×倍數",
        )
        wf_atr_tp = st.slider(
            "ATR 目標倍數",
            min_value=1.0, max_value=5.0, value=3.0, step=0.25, key="wf_atr_tp",
            help="目標線 = 進場價 + ATR×倍數",
        )
        if wf_exit_mode == "simple":
            wf_scan = 1
            st.caption("💡 簡化模式固定每日掃描（scan_freq=1）")
        else:
            wf_scan = st.slider(
                "進場掃描頻率 (日)",
                min_value=1, max_value=10, value=5, step=1, key="wf_scan",
                help="每 N 日掃描一次進場訊號（進階模式可調整）",
            )

        wf_run = st.button("🚀 執行 Walk-Forward 回測", type="primary", key="wf_run")

    with wf_col2:
        if not wf_run:
            return
        if wf_start >= wf_end:
            st.error("結束日期必須晚於開始日期")
            return

        with st.spinner("執行 Walk-Forward 逐日回測..."):
            bt = WalkForwardBacktester()
            wf_result = bt.run_walkforward(
                df=btc,
                start_date=wf_start.strftime("%Y-%m-%d"),
                end_date=wf_end.strftime("%Y-%m-%d"),
                initial_capital=wf_init_cap,
                scan_freq=wf_scan,
                exit_ma=wf_exit_ma,
                entry_dist_min_pct=wf_dist,
                entry_dist_max_pct=wf_dist_max,
                rsi_min=wf_rsi,
                adx_min=wf_adx,
                exit_mode=wf_exit_mode,
                atr_sl_multiplier=wf_atr_sl,
                atr_tp_multiplier=wf_atr_tp,
            )

        wf_m1, wf_m2, wf_m3, wf_m4, wf_m5 = st.columns(5)
        wf_m1.metric("最終資產", f"${wf_result['final_balance']:,.0f}")
        wf_m2.metric("策略報酬", f"{wf_result['stock_return']:+.2f}%", delta_color="normal")
        wf_m3.metric("Buy&Hold", f"{wf_result['benchmark_return']:+.2f}%")
        wf_m4.metric("Alpha", f"{wf_result['alpha']:+.2f}%")
        wf_m5.metric("交易次數", f"{wf_result['trade_count']} 次")

        st.markdown("---")
        wf_s1, wf_s2, wf_s3, wf_s4 = st.columns(4)
        wf_s1.metric("勝率", f"{wf_result['win_rate']:.1f}%")
        wf_s2.metric("Sharpe Ratio", f"{wf_result['sharpe']:.2f}")
        wf_s3.metric("最大回撤", f"{wf_result['max_drawdown']:.2f}%", delta_color="inverse")

        if not wf_result['trades'].empty:
            wf_trades = wf_result['trades']
            wf_mask = (btc.index >= pd.Timestamp(wf_start)) & (btc.index <= pd.Timestamp(wf_end))
            wf_sub = btc.loc[wf_mask]

            fig_wf = go.Figure()
            fig_wf.add_trace(go.Scatter(
                x=wf_sub.index, y=wf_sub['close'],
                mode='lines', name='BTC 價格',
                line=dict(color='gray', width=1),
            ))

            if wf_exit_ma in wf_sub.columns:
                fig_wf.add_trace(go.Scatter(
                    x=wf_sub.index, y=wf_sub[wf_exit_ma],
                    mode='lines', name=f'{wf_exit_ma} (防守線)',
                    line=dict(color='orange', width=1.5, dash='dash'),
                ))

            entry_dates  = pd.to_datetime(wf_trades['entry_date'])
            entry_prices = wf_trades['entry_price'].values
            fig_wf.add_trace(go.Scatter(
                x=entry_dates, y=entry_prices,
                mode='markers+text', name='進場',
                marker=dict(color='#00ff88', symbol='triangle-up', size=10),
                text=[f"+{p:.0f}" for p in wf_trades['pnl_pct'].values],
                textposition='top center',
            ))

            exit_dates  = pd.to_datetime(wf_trades['exit_date'])
            exit_prices = wf_trades['exit_price'].values
            fig_wf.add_trace(go.Scatter(
                x=exit_dates, y=exit_prices,
                mode='markers', name='出場',
                marker=dict(color='#ff4b4b', symbol='triangle-down', size=10),
            ))

            fig_wf.update_layout(
                title=f"Walk-Forward 回測買賣點（{wf_start}～{wf_end}）",
                height=500, template="plotly_dark",
                hovermode='x unified',
            )
            st.plotly_chart(fig_wf, use_container_width=True)

            with st.expander("📋 詳細交易紀錄"):
                st.dataframe(wf_trades, use_container_width=True)
            st.download_button(
                label="⬇️ 下載 Walk-Forward 交易紀錄 (.csv)",
                data=df_to_csv_bytes(wf_trades),
                file_name=f"walkforward_trades_{wf_start}_{wf_end}.csv",
                mime="text/csv", key="wf_download",
            )
        else:
            st.warning("⚠️ 此期間無任何進場訊號。可嘗試放寬進場條件或延長回測區間。")

        with st.expander("📖 Walk-Forward 邏輯說明"):
            st.markdown(f"""
            **核心設計（無先視偏誤）**：
            - 每日逐一推進，該日只能看到該日及以前的資料
            - 訊號在「前一日」收盤確認，「當日」開盤執行（shift(1) 防護）
            - 進場掃描頻率：每 {wf_scan} 日掃描一次（降低計算成本）
            - 持倉中每日檢查出場條件（多層次）

            **進場條件（五合一）**：
            1. 價格 > SMA200（年線多頭）
            2. RSI14 > {wf_rsi}（動能）
            3. EMA20 乖離 ≥ {wf_dist:.1f}%{"（無上限）" if wf_dist_max is None else f"且 ≤ {wf_dist_max:.1f}%"}（甜蜜點）
            4. MACD > Signal（多頭交叉）
            5. ADX > {wf_adx}（趨勢強度）

            **六層出場機制（優先級）**：
            ① Climax Exit：正乖離>30% 或爆量+長上影線（隔日強制）
            ② ATR 停損：跌破進場 - {wf_atr_sl:.2f}×ATR
            ③ ATR 目標：達到進場 + {wf_atr_tp:.2f}×ATR
            ④ Chandelier：最高 - 2.0×ATR（追蹤止利）
            ⑤ Time Stop：持倉≥15日 且 淨報酬<5%
            ⑥ EMA 停損：跌破 {wf_exit_ma}

            **交易成本**：
            - 進場：費用 + 滑點 ≈ 0.15%
            - 出場：費用 + 滑點 ≈ 0.15%
            - 往返摩擦成本 ≈ 0.3%
            """)
