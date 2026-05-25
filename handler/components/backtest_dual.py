"""
Backtest Sub-Tab 2：雙幣理財長期滾倉回測

從 handler/tab_backtest.py 拆分。
"""
import streamlit as st
import plotly.graph_objects as go

from strategy.dual_invest import run_dual_investment_backtest
from handler.components._utils import df_to_csv_bytes


def render(btc, call_risk=None, put_risk=None):
    """Sub-Tab 2: 雙幣滾倉回測"""
    st.markdown("#### 💰 雙幣理財長期滾倉回測")

    di_col1, di_col2 = st.columns(2)
    with di_col1:
        _call_risk = st.number_input(
            "Sell High 風險係數",
            value=float(call_risk) if call_risk is not None else 0.5,
            step=0.1, min_value=0.1, max_value=2.0,
            help="越大掛越遠（越保守），決定行權價距離現價的倍數",
        )
    with di_col2:
        _put_risk = st.number_input(
            "Buy Low 風險係數",
            value=float(put_risk) if put_risk is not None else 0.5,
            step=0.1, min_value=0.1, max_value=2.0,
            help="越大掛越遠（越保守），決定行權價距離現價的倍數",
        )

    if st.button("🚀 執行滾倉回測"):
        with st.spinner("正在模擬每日滾倉..."):
            logs = run_dual_investment_backtest(btc, call_risk=_call_risk, put_risk=_put_risk)
            if not logs.empty:
                m1, m2 = st.columns(2)
                final_eq = logs.iloc[-1]['Equity_BTC']
                ret = (final_eq - 1) * 100
                m1.metric("最終權益 (BTC)", f"{final_eq:.4f}", f"{ret:.2f}%")
                m2.metric("總交易次數", f"{len(logs[logs['Action'] == 'Open'])} 次")
                fig2 = go.Figure()
                fig2.add_trace(go.Scatter(
                    x=logs['Time'], y=logs['Equity_BTC'],
                    mode='lines', name='Equity (BTC)', line=dict(color='#00ff88'),
                ))
                fig2.update_layout(
                    title="資產淨值走勢 (BTC本位)", height=400, template="plotly_dark"
                )
                st.plotly_chart(fig2, use_container_width=True)
                with st.expander("詳細交易日誌"):
                    st.dataframe(logs)
                st.download_button(
                    label="⬇️ 下載雙幣滾倉日誌 (.csv)",
                    data=df_to_csv_bytes(logs),
                    file_name="dual_invest_trade_log.csv",
                    mime="text/csv",
                )
            else:
                st.warning("無交易紀錄")
