"""
Backtest Sub-Tab 4：多週期回測 (Multi-Timeframe)

從 handler/tab_backtest.py 拆分。日線 + 15m 多週期回測。
"""
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from datetime import timedelta

from strategy.swing import run_multitf_backtest
from service.local_db_reader import read_btc_15m, has_local_data
from config import DEFAULT_INITIAL_CAPITAL
from handler.components._utils import df_to_csv_bytes


def render(btc):
    """Sub-Tab 4: 多週期回測"""
    st.markdown("#### 📈 多週期回測 (Multi-Timeframe)")
    st.caption(
        "🛡️ **防先視偏誤**：日線條件在第 N 日收盤後確認，第 N+1 日起的 15m 才允許進場；"
        "15m 訊號在第 M 根收盤後確認，第 M+1 根開盤執行。"
    )

    if not has_local_data():
        st.error("❌ 找不到本地 15m SQLite 資料庫（db/ 目錄）。請先執行 collector/btc_price_collector.py 收集資料。")
        return

    mt_col1, mt_col2 = st.columns([1, 3])

    with mt_col1:
        st.subheader("⚙️ 多週期設定")

        min_date = btc.index[0].date()
        max_date = btc.index[-1].date()
        mt_start = st.date_input(
            "開始日期", value=max_date - timedelta(days=365),
            min_value=min_date, max_value=max_date, key="mt_start",
        )
        mt_end = st.date_input(
            "結束日期", value=max_date,
            min_value=min_date, max_value=max_date, key="mt_end",
        )
        mt_cap = st.number_input(
            "初始本金 (USDT)", value=int(DEFAULT_INITIAL_CAPITAL),
            step=1_000, key="mt_cap",
        )

        st.markdown("**日線宏觀過濾**")
        use_sma200 = st.checkbox("收盤 > SMA200（年線多頭）", value=True)
        use_golden = st.checkbox("SMA50 > SMA200（金叉確認）", value=False)

        st.markdown("**15m 進場條件**")
        ema_period = st.selectbox("15m EMA 週期", [10, 20, 50], index=1)
        rsi_15m    = st.slider("15m RSI 動能閾值", 40, 65, 50, key="mt_rsi")
        stop_pct   = st.slider(
            "停損 (%)", 1.0, 10.0, 3.0, step=0.5,
            help="進場後收盤跌破此百分比即觸發停損出場",
        )

        run_mt = st.button("🚀 執行多週期回測", type="primary", key="mt_run")

    with mt_col2:
        if not run_mt:
            return
        if mt_start >= mt_end:
            st.error("結束日期必須晚於開始日期")
            return

        days_span = (mt_end - mt_start).days
        if days_span > 730:
            st.warning(f"⚠️ 回測區間 {days_span} 天，15m 資料量較大，計算需數秒，請耐心等候。")

        with st.spinner("載入 15m 資料並執行多週期回測..."):
            df_15m = read_btc_15m(
                start_date=mt_start.strftime("%Y-%m-%d"),
                end_date=mt_end.strftime("%Y-%m-%d"),
            )

        if df_15m.empty:
            st.error("❌ 無法取得此期間的 15m 資料，請確認本地 DB 已更新。")
            return

        with st.spinner("模擬多週期交易..."):
            mt_trades, mt_final, mt_roi, mt_n, mt_mdd, mt_stats = run_multitf_backtest(
                df_daily=btc,
                df_15m=df_15m,
                start_date=mt_start,
                end_date=mt_end,
                initial_capital=mt_cap,
                daily_use_sma200=use_sma200,
                daily_use_golden=use_golden,
                ema_period_15m=ema_period,
                rsi_min_15m=rsi_15m,
                stop_loss_pct=stop_pct,
            )

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("最終資產", f"${mt_final:,.0f}")
        m2.metric("總報酬率 (ROI)", f"{mt_roi:+.2f}%", delta_color="normal")
        try:
            start_price = btc.loc[pd.Timestamp(mt_start):]['close'].iloc[0]
            end_price   = btc.loc[:pd.Timestamp(mt_end)]['close'].iloc[-1]
            bh_roi = (end_price / start_price - 1) * 100
            m3.metric("Buy & Hold 報酬", f"{bh_roi:+.2f}%")
        except Exception:
            m3.metric("Buy & Hold 報酬", "—")
        m4.metric("最大回撤 (MDD)", f"{mt_mdd:.2f}%", delta_color="inverse")
        m5.metric("交易次數 (15m)", f"{mt_n} 次")

        st.markdown("---")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("勝率", f"{mt_stats.get('win_rate', 0):.1f}%")
        s2.metric("Sharpe (年化)", f"{mt_stats.get('sharpe', 0):.2f}")
        s3.metric("平均獲利", f"{mt_stats.get('avg_profit', 0):+.2f}%", delta_color="normal")
        s4.metric("平均虧損", f"{mt_stats.get('avg_loss', 0):+.2f}%", delta_color="inverse")

        if not mt_trades.empty:
            mask   = (btc.index >= pd.Timestamp(mt_start)) & (btc.index <= pd.Timestamp(mt_end))
            sub_df = btc.loc[mask]
            fig_mt = go.Figure()
            fig_mt.add_trace(go.Scatter(
                x=sub_df.index, y=sub_df['close'],
                mode='lines', name='BTC (日線)',
                line=dict(color='gray', width=1),
            ))
            if 'SMA_200' in sub_df.columns:
                fig_mt.add_trace(go.Scatter(
                    x=sub_df.index, y=sub_df['SMA_200'],
                    mode='lines', name='SMA200 (日)',
                    line=dict(color='orange', width=1, dash='dot'),
                ))

            buys  = mt_trades[mt_trades['Type'] == 'Buy']
            sells = mt_trades[mt_trades['Type'] == 'Sell']
            if not buys.empty:
                fig_mt.add_trace(go.Scatter(
                    x=buys['Date'], y=buys['Price'], mode='markers',
                    name='15m 買入',
                    marker=dict(color='#00ff88', symbol='triangle-up', size=7),
                ))
            if not sells.empty:
                stop_sells = sells[sells['Reason'] == 'Stop Loss']
                norm_sells = sells[sells['Reason'] != 'Stop Loss']
                if not norm_sells.empty:
                    fig_mt.add_trace(go.Scatter(
                        x=norm_sells['Date'], y=norm_sells['Price'], mode='markers',
                        name='15m 賣出',
                        marker=dict(color='#ff4b4b', symbol='triangle-down', size=7),
                    ))
                if not stop_sells.empty:
                    fig_mt.add_trace(go.Scatter(
                        x=stop_sells['Date'], y=stop_sells['Price'], mode='markers',
                        name='停損出場',
                        marker=dict(color='#ff9900', symbol='x', size=8),
                    ))

            fig_mt.update_layout(
                title="多週期回測買賣點（日線圖疊加 15m 訊號）",
                height=500, template="plotly_dark",
            )
            st.plotly_chart(fig_mt, use_container_width=True)

            with st.expander("交易明細"):
                st.dataframe(mt_trades, use_container_width=True)
            st.download_button(
                label="⬇️ 下載多週期交易紀錄 (.csv)",
                data=df_to_csv_bytes(mt_trades),
                file_name=f"multitf_trades_{mt_start}_{mt_end}.csv",
                mime="text/csv",
            )
        else:
            st.warning("⚠️ 此期間無任何進場訊號。可嘗試：放寬日線過濾條件、降低 RSI 閾值，或延長回測區間。")

        with st.expander("📖 多週期策略邏輯說明"):
            st.markdown(f"""
            **日線宏觀過濾（今日生效的是昨日確認的條件）**：
            {'- ✅ 收盤 > SMA200（年線多頭）' if use_sma200 else '- ⬜ SMA200 過濾已停用'}
            {'- ✅ SMA50 > SMA200（金叉確認）' if use_golden else '- ⬜ 金叉過濾已停用'}

            **15m 進場條件**（訊號收盤確認，次根開盤執行）：
            - 15m 收盤 > 15m EMA{ema_period}
            - 15m RSI14 > {rsi_15m}

            **15m 出場條件**：
            - 15m 收盤 < 15m EMA{ema_period}（趨勢轉弱）
            - 固定停損：進場後收盤跌破 {stop_pct}%

            **防先視偏誤**：所有訊號均已 shift(1)，確保使用的是「已知」資訊。
            """)
