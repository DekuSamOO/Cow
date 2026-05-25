"""
Backtest Sub-Tab 1：波段策略 PnL（含參數最佳化網格搜尋）

從 handler/tab_backtest.py 拆分，行為等同 v2.0。
"""
import itertools
import streamlit as st
import plotly.graph_objects as go
import pandas as pd
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from strategy.swing import run_swing_strategy_backtest
from config import DEFAULT_INITIAL_CAPITAL
from handler.components._utils import df_to_csv_bytes


def render(btc):
    """Sub-Tab 1: 波段策略 PnL"""
    st.markdown("#### 📉 波段策略驗證 (自訂區間 PnL)")
    b_col1, b_col2 = st.columns([1, 3])

    with b_col1:
        st.subheader("⚙️ 回測設定")
        min_date = btc.index[0].date()
        max_date = btc.index[-1].date()
        start_d = st.date_input(
            "開始日期", value=min_date + timedelta(days=365),
            min_value=min_date, max_value=max_date,
        )
        end_d = st.date_input(
            "結束日期", value=max_date,
            min_value=min_date, max_value=max_date,
        )
        init_cap = st.number_input(
            "初始本金 (USDT)",
            value=int(DEFAULT_INITIAL_CAPITAL),
            step=1_000,
        )

        st.markdown("---")
        st.markdown("**進場與防守條件調整**")
        dist_min = st.slider(
            "EMA20 最小乖離 (%)",
            min_value=0.0, max_value=2.0, value=0.0, step=0.1,
            help="收盤價高於 EMA20 的最小百分比偏差（0 = 只要站上 EMA20 即符合）",
        )
        rsi_thresh = st.slider(
            "RSI 動能閾值",
            min_value=40, max_value=65, value=50, step=1,
            help="RSI 需高於此值才視為多頭動能",
        )
        adx_thresh = st.slider(
            "ADX 趨勢強度閾值",
            min_value=10, max_value=35, value=20, step=1,
            help="ADX 需高於此值才視為有效趨勢（過濾橫盤假訊號）",
        )

        exit_ma_key = st.selectbox(
            "波段防守線 (出場條件)",
            options=["SMA_50", "EMA_20", "SMA_200"],
            index=0,
            help="選擇做為出場防守的均線。當價格跌破此均線即觸發賣出。"
        )

        run_backtest = st.button("🚀 執行波段回測", type="primary")

        st.markdown("---")
        st.markdown("**🔍 參數最佳化**")
        st.caption("迴圈搜尋「勝率最高」或「報酬最佳」的參數組合")
        opt_metric = st.radio(
            "最佳化目標",
            options=["最高勝率 (Win Rate)", "最高總報酬 (ROI)"],
            index=0, horizontal=True,
        )
        run_optimize = st.button("🔬 尋找最佳參數", help="需要數秒鐘，請耐心等候")

    with b_col2:
        if run_backtest:
            if start_d >= end_d:
                st.error("結束日期必須晚於開始日期")
            else:
                with st.spinner("正在模擬交易..."):
                    trades, final_val, roi, num_trades, mdd, stats = run_swing_strategy_backtest(
                        btc, start_d, end_d, init_cap,
                        entry_dist_min_pct=dist_min,
                        rsi_min=rsi_thresh,
                        adx_min=adx_thresh,
                        exit_ma=exit_ma_key,
                    )
                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("最終資產", f"${final_val:,.0f}")
                    m2.metric("總報酬率 (ROI)", f"{roi:+.2f}%", delta_color="normal")
                    start_price = btc.loc[pd.Timestamp(start_d):]['close'].iloc[0]
                    end_price   = btc.loc[:pd.Timestamp(end_d)]['close'].iloc[-1]
                    bh_roi = (end_price / start_price - 1) * 100
                    m3.metric("Buy & Hold 報酬", f"{bh_roi:+.2f}%")
                    m4.metric("最大回撤 (MDD)", f"{mdd:.2f}%", delta_color="inverse")
                    m5.metric("總交易", f"{num_trades} 次")

                    st.markdown("---")
                    s1, s2, s3, s4 = st.columns(4)
                    s1.metric("勝率 (Win Rate)", f"{stats['win_rate']:.1f}%")
                    s2.metric("Sharpe Ratio", f"{stats['sharpe']:.2f}")
                    s3.metric("平均獲利", f"{stats['avg_profit']:+.2f}%", delta_color="normal")
                    s4.metric("平均虧損", f"{stats['avg_loss']:+.2f}%", delta_color="inverse")

                    mask   = (btc.index >= pd.Timestamp(start_d)) & (btc.index <= pd.Timestamp(end_d))
                    sub_df = btc.loc[mask]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=sub_df.index, y=sub_df['close'],
                        mode='lines', name='Price', line=dict(color='gray', width=1),
                    ))
                    if exit_ma_key in sub_df.columns:
                        fig.add_trace(go.Scatter(
                            x=sub_df.index, y=sub_df[exit_ma_key],
                            mode='lines', name=f'{exit_ma_key} (防守線)',
                            line=dict(color='yellow', width=1, dash='dash'),
                        ))
                    if not trades.empty:
                        buys  = trades[trades['Type'] == 'Buy']
                        sells = trades[trades['Type'] == 'Sell']
                        fig.add_trace(go.Scatter(
                            x=buys['Date'], y=buys['Price'], mode='markers', name='Buy',
                            marker=dict(color='#00ff88', symbol='triangle-up', size=10),
                        ))
                        fig.add_trace(go.Scatter(
                            x=sells['Date'], y=sells['Price'], mode='markers', name='Sell',
                            marker=dict(color='#ff4b4b', symbol='triangle-down', size=10),
                        ))
                    fig.update_layout(title="波段交易買賣點回放", height=500, template="plotly_dark")
                    st.plotly_chart(fig, use_container_width=True)

                    if not trades.empty:
                        with st.expander("交易明細"):
                            st.dataframe(trades)
                        st.download_button(
                            label="⬇️ 下載波段交易紀錄 (.csv)",
                            data=df_to_csv_bytes(trades),
                            file_name=f"swing_trades_{start_d}_{end_d}.csv",
                            mime="text/csv",
                        )

        if run_optimize:
            if start_d >= end_d:
                st.error("結束日期必須晚於開始日期")
            else:
                st.info("🔬 開始網格搜尋，掃描參數組合中...")

                dist_min_range  = [0.0, 0.2, 0.5]
                rsi_range       = [45, 50, 55]
                adx_range       = [15, 20, 25]
                exit_ma_range   = ["SMA_50", "EMA_20", "SMA_200"]

                grid = list(itertools.product(dist_min_range, rsi_range, adx_range, exit_ma_range))

                best_params = None
                best_metric_val = -float('inf')
                results = []

                progress_bar = st.progress(0)
                total = len(grid)
                completed_count = [0]

                def _run_one(params):
                    dmin, rsi, adx, ema_exit = params
                    _, fval, roi_v, ntrades, _, sts = run_swing_strategy_backtest(
                        btc, start_d, end_d, init_cap,
                        entry_dist_min_pct=dmin,
                        rsi_min=rsi,
                        adx_min=adx,
                        exit_ma=ema_exit,
                    )
                    return params, roi_v, ntrades, sts

                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {executor.submit(_run_one, p): p for p in grid}
                    for future in as_completed(futures):
                        (dmin, rsi, adx, ema_exit), roi_v, ntrades, sts = future.result()
                        target_val = sts.get('win_rate', 0) if "勝率" in opt_metric else roi_v
                        row = {
                            "EMA乖離Min(%)": dmin,
                            "RSI閾值": rsi,
                            "ADX閾值": adx,
                            "防守線": ema_exit,
                            "勝率(%)": round(sts.get('win_rate', 0), 1),
                            "總報酬ROI(%)": round(roi_v, 2),
                            "Sharpe": round(sts.get('sharpe', 0), 2),
                            "交易次數": ntrades,
                        }
                        results.append(row)
                        if target_val > best_metric_val and ntrades >= 3:
                            best_metric_val = target_val
                            best_params = row
                        completed_count[0] += 1
                        progress_bar.progress(min(completed_count[0] / total, 1.0))

                progress_bar.empty()

                if best_params:
                    st.success(f"✅ 找到最佳參數！（最佳化目標：{opt_metric}）")
                    bp_cols = st.columns(4)
                    bp_cols[0].metric("EMA乖離Min", f"{best_params['EMA乖離Min(%)']}%")
                    bp_cols[1].metric("RSI / ADX",    f"{best_params['RSI閾值']} / {best_params['ADX閾值']}")
                    bp_cols[2].metric("最佳防守線",    f"{best_params['防守線']}")
                    bp_cols[3].metric("勝率 / ROI",  f"{best_params['勝率(%)']}% / {best_params['總報酬ROI(%)']:+.1f}%")
                else:
                    st.warning("⚠️ 在所有參數組合中，交易次數均不足 3 次，無法評估。請調整日期範圍。")

                results_df = pd.DataFrame(results)
                sort_col   = "勝率(%)" if "勝率" in opt_metric else "總報酬ROI(%)"
                results_df = results_df.sort_values(sort_col, ascending=False).head(10)
                with st.expander("📊 Top 10 參數組合結果", expanded=True):
                    st.dataframe(results_df, use_container_width=True, hide_index=True)
