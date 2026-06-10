"""
handler/tab_backtest.py  ·  v2.1
Tab 4: 時光機回測 — 純編排檔

v2.1: 5 個 sub-tab 內容拆至 handler/components/backtest_*.py
v2.0: 所有策略參數移至 Tab 內部設定；bt_tab1 新增最佳化按鈕；bt_tab3 修正 MA200+MA50 同繪
"""
# 關閉 SSL 驗證警告，避免本地端公司網路環境報錯
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import streamlit as st

from handler.components import (
    backtest_swing,
    backtest_dual,
    backtest_bull_radar,
    backtest_multitf,
    backtest_walkforward,
    backtest_radar,
)


def render(btc, call_risk=None, put_risk=None, ahr_threshold=None):
    """
    回測 Tab 渲染入口

    call_risk / put_risk / ahr_threshold 仍允許從外部傳入作為預設值（兼容舊呼叫），
    但實際參數調整都在各 sub-tab 內部 UI。
    """
    st.markdown("### ⏳ 時光機回測 (Backtest Engine)")

    bt_tab1, bt_tab2, bt_tab3, bt_tab4, bt_tab5, bt_tab6 = st.tabs([
        "📉 波段策略 PnL",
        "💰 雙幣滾倉回測",
        "🐂 牛市雷達準確度",
        "📈 多週期回測 (Multi-TF)",
        "🚀 Walk-Forward 無先視回測",
        "📡 波段雷達回放",
    ])

    with bt_tab1:
        backtest_swing.render(btc)

    with bt_tab2:
        backtest_dual.render(btc, call_risk=call_risk, put_risk=put_risk)

    with bt_tab3:
        backtest_bull_radar.render(btc, ahr_threshold=ahr_threshold)

    with bt_tab4:
        backtest_multitf.render(btc)

    with bt_tab5:
        backtest_walkforward.render(btc)

    with bt_tab6:
        backtest_radar.render(btc)
