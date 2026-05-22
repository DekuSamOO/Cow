"""
handler/tab_bull_radar.py  ·  v1.1
Tab 1: 牛市雷達 (Bull Detector)

版次記錄:
  v1.0  初版（含 Task #7 Session State 圖表快取）
  v1.1  [本次] 修正以下問題：
        ① Level 3 DXY / M2 / CPI / JPY：全部加 fallback 備援
          失敗時顯示最近已知靜態值（附日期標記 ⚠️），而非空白「—」
        ② AHR999 卡片：help tooltip 補充 SMA200 + PowerLaw 計算明細
          讓用戶能即時驗證數值來源
        ③ Level 3 DXY：is_fallback 旗標判斷，避免 tz-aware 比較問題

[Task #7] Session State 圖表快取:
  - cache_key = MD5(最後時間戳 + 資料筆數)[:16]
  - 側邊欄操作不觸發重建，只有新資料才重建
  - 效果: 200-500ms → <5ms
"""
import hashlib
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
from datetime import datetime

from service.macro_data import fetch_m2_series, fetch_usdjpy, fetch_us_cpi_yoy, get_quantum_threat_level


# ──────────────────────────────────────────────────────────────────────────────
# Fallback 靜態數據（service/macro_data.py 連線失敗時使用）
# 每月人工更新一次即可。最後更新: 2025-02-25
# ──────────────────────────────────────────────────────────────────────────────
_FALLBACK = {
    "dxy":    {"value": 106.5,  "date": "2025-02-21"},
    "m2":     {"value": 21450,  "date": "2025-01-01"},
    "cpi":    {"value": 3.0,    "date": "2025-01-01"},
    "usdjpy": {"value": 150.5,  "date": "2025-02-21"},
}


def _make_chart_cache_key(chart_df, tvl_hist, stable_hist, fund_hist) -> str:
    parts = [
        str(chart_df.index[-1])    if not chart_df.empty    else "empty",
        str(len(chart_df)),
        str(tvl_hist.index[-1])    if not tvl_hist.empty    else "empty",
        str(stable_hist.index[-1]) if not stable_hist.empty else "empty",
        str(fund_hist.index[-1])   if not fund_hist.empty   else "empty",
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]


def render(btc, chart_df, tvl_hist, stable_hist, fund_hist, curr, dxy,
           funding_rate, tvl_val, fng_val, fng_state, fng_source, proxies, realtime_data):
    st.subheader("BTCUSDT 多維度綜合分析 (Multi-Dimension Analysis)")

    # ── [Task #7] 主圖表快取 ──────────────────────────────────────────────────
    cache_key   = _make_chart_cache_key(chart_df, tvl_hist, stable_hist, fund_hist)
    ss_fig_key  = f"tab_bull_fig_{cache_key}"
    ss_hash_key = "tab_bull_fig_key"

    if (st.session_state.get(ss_hash_key) == cache_key
            and ss_fig_key in st.session_state):
        fig_t1 = st.session_state[ss_fig_key]
    else:
        if chart_df.index.tz is not None:
            chart_df = chart_df.copy()
            chart_df.index = chart_df.index.tz_localize(None)

        fig_t1 = make_subplots(
            rows=5, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.025,
            row_heights=[0.40, 0.15, 0.15, 0.15, 0.15],
            subplot_titles=(
                "比特幣價格行為 (Price Action)",
                "AHR999 囤幣指標 (< 0.45 = 歷史抄底區)",
                "幣安資金費率 (Funding Rate) & RSI_14",
                "BTC 鏈上 TVL (DeFiLlama)",
                "全球穩定幣市值 (Stablecoin Cap)",
            ),
        )

        # Row 1: 價格 + 均線
        fig_t1.add_trace(go.Candlestick(
            x=chart_df.index, open=chart_df['open'], high=chart_df['high'],
            low=chart_df['low'], close=chart_df['close'], name='BTC',
        ), row=1, col=1)
        fig_t1.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['SMA_200'],
            line=dict(color='orange', width=2), name='SMA 200',
        ), row=1, col=1)
        fig_t1.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['SMA_50'],
            line=dict(color='cyan', width=1, dash='dash'), name='SMA 50',
        ), row=1, col=1)
        if 'EMA_20' in chart_df.columns:
            fig_t1.add_trace(go.Scatter(
                x=chart_df.index, y=chart_df['EMA_20'],
                line=dict(color='#ffeb3b', width=1, dash='dot'), name='EMA 20',
            ), row=1, col=1)

        # Row 2: AHR999
        if 'AHR999' in chart_df.columns and chart_df['AHR999'].notna().any():
            ahr_colors = [
                '#00ff88' if v < 0.45
                else ('#ffcc00' if v < 0.8
                else ('#ff8800' if v < 1.2
                else '#ff4b4b'))
                for v in chart_df['AHR999'].fillna(1.0)
            ]
            fig_t1.add_trace(go.Bar(
                x=chart_df.index, y=chart_df['AHR999'],
                marker_color=ahr_colors, name='AHR999', showlegend=False,
            ), row=2, col=1)
            for lvl, col, lbl in [
                (0.45, '#00ff88', '抄底 0.45'),
                (0.8,  '#ffcc00', '偏低 0.8'),
                (1.2,  '#ff4b4b', '高估 1.2'),
            ]:
                fig_t1.add_hline(y=lvl, line_color=col, line_width=1, line_dash='dash',
                                 annotation_text=lbl, row=2, col=1)

        # Row 3: 資金費率 + RSI
        if not fund_hist.empty:
            fund_sub  = fund_hist.reindex(chart_df.index, method='nearest')
            fr_colors = ['#00ff88' if v > 0 else '#ff4b4b' for v in fund_sub['fundingRate']]
            fig_t1.add_trace(go.Bar(
                x=fund_sub.index, y=fund_sub['fundingRate'],
                marker_color=fr_colors, name='Funding Rate %',
            ), row=3, col=1)
        if 'RSI_14' in chart_df.columns and chart_df['RSI_14'].notna().any():
            rsi_scaled = (chart_df['RSI_14'] - 50) * 0.001
            fig_t1.add_trace(go.Scatter(
                x=chart_df.index, y=rsi_scaled,
                line=dict(color='#a32eff', width=1.5), name='RSI (scaled)',
            ), row=3, col=1)
        fig_t1.add_hline(y=0.03, line_color='#ff4b4b', line_width=0.8,
                         line_dash='dot', annotation_text="過熱 0.03%", row=3, col=1)

        # Row 4: TVL
        if not tvl_hist.empty:
            if tvl_hist.index.tz is not None:
                tvl_hist = tvl_hist.copy()
                tvl_hist.index = tvl_hist.index.tz_localize(None)
            tvl_sub = tvl_hist.reindex(chart_df.index, method='nearest')
            fig_t1.add_trace(go.Scatter(
                x=tvl_sub.index,
                y=tvl_sub['tvl'] if 'tvl' in tvl_sub.columns else [],
                mode='lines', fill='tozeroy',
                line=dict(color='#a32eff'), name='TVL (USD)',
            ), row=4, col=1)

        # Row 5: 穩定幣市值
        if not stable_hist.empty:
            stab_sub = stable_hist.reindex(chart_df.index, method='nearest')
            fig_t1.add_trace(go.Scatter(
                x=stab_sub.index, y=stab_sub['mcap'] / 1e9,
                mode='lines', line=dict(color='#2E86C1'), name='Stablecoin Cap ($B)',
            ), row=5, col=1)

        fig_t1.update_layout(
            height=1000, template="plotly_dark", xaxis_rangeslider_visible=False,
            legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='right', x=1),
        )
        st.session_state[ss_fig_key]  = fig_t1
        st.session_state[ss_hash_key] = cache_key

    st.plotly_chart(fig_t1, width='stretch')

    # ── 市場相位判定 ──────────────────────────────────────────────────────────
    price       = curr['close']
    ma50        = curr['SMA_50']
    ma200       = curr['SMA_200']
    ma200_slope = curr.get('SMA_200_Slope', 0)
    mvrv        = curr.get('MVRV_Z_Proxy', 0)

    if mvrv > 3.5:
        phase_name, phase_desc = "🔥 狂熱頂部 (Overheated)", "風險極高，建議分批止盈"
    elif price > ma200 and ma50 > ma200 and ma200_slope > 0:
        phase_name, phase_desc = "🐂 牛市主升段 (Bull Run)", "趨勢多頭排列且年線上揚，主升段"
    elif price > ma200 and ma50 > ma200 and ma200_slope <= 0:
        phase_name, phase_desc = "😴 牛市休整/末期 (Stagnant Bull)", "價格雖高但年線走平，動能減弱"
    elif price > ma200 and ma50 <= ma200:
        phase_name, phase_desc = "🌱 初牛復甦 (Recovering)", "價格站上年線，等待黃金交叉與年線翻揚"
    elif price <= ma200 and ma50 > ma200:
        phase_name, phase_desc = "📉 轉折回調 (Correction)", "跌破年線，需注意是否死叉"
    else:
        phase_name, phase_desc = "❄️ 深熊築底 (Winter)", "均線空頭排列，定投積累區"

    st.info(f"### 📡 當前市場相位：**{phase_name}**\n\n{phase_desc}")
    st.markdown("---")

    # ── 三層分析框架 ──────────────────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)

    # ── Level 1: 散戶視角 ─────────────────────────────────────────────────────
    with col1:
        st.markdown("### Level 1: 散戶視角")
        is_golden = (curr['close'] > curr['SMA_200']) and (curr['SMA_50'] > curr['SMA_200'])
        is_rising = curr.get('SMA_200_Slope', 0) > 0
        struct_state = (
            "多頭共振 (STRONG)" if (is_golden and is_rising)
            else ("震盪/修正 (WEAK)" if not is_golden else "年線走平 (FLAT)")
        )
        st.metric(
            "趨勢結構 (Structure)", struct_state,
            delta=f"MA200 斜率 {('↗️ 上升' if is_rising else '↘️ 下降')}",
            delta_color="normal" if is_rising else "off",
        )
        recent_high = btc['high'].iloc[-20:].max()
        prev_high   = btc['high'].iloc[-40:-20].max()
        dow_state   = "更高的高點 (HH)" if recent_high > prev_high else "高點降低 (LH)"
        st.metric("道氏理論結構", dow_state)
        st.metric(f"情緒指數 ({fng_source})", f"{fng_val:.0f}/100", fng_state)

    # ── Level 2: 機構視角 ─────────────────────────────────────────────────────
    with col2:
        st.markdown("### Level 2: 機構視角")
        ahr_val = curr['AHR999']
        ahr_state = (
            "🟢 抄底區間 (歷史大底)" if ahr_val < 0.45
            else ("🟡 合理區間 (持有)" if ahr_val < 1.2 else "🔴 高估區間 (分批止盈)")
        )

        # ▸ v1.1: tooltip 補充 SMA200 + PowerLaw 計算明細，方便驗證
        genesis_date = datetime(2009, 1, 3)
        today_dt     = datetime.utcnow()
        days_genesis = max((today_dt - genesis_date).days, 1)
        power_law_val = 10 ** (-17.01467 + 5.84 * np.log10(days_genesis))
        sma200_val    = curr.get('SMA_200', float('nan'))
        ahr_tooltip = (
            f"公式: AHR999 = (Price/SMA200) × (Price/PowerLaw)\n"
            f"─────────────────────────\n"
            f"當前 Price   = ${curr['close']:,.0f}\n"
            f"SMA 200      = ${sma200_val:,.0f}\n"
            f"PowerLaw     = ${power_law_val:,.0f}  (Giovanni Santostasi 冪律模型)\n"
            f"─────────────────────────\n"
            f"Price/SMA200 = {curr['close']/sma200_val:.4f}\n"
            f"Price/PL     = {curr['close']/power_law_val:.4f}\n"
            f"AHR999       = {ahr_val:.4f}\n"
            f"─────────────────────────\n"
            f"< 0.45 抄底 | 0.45-1.2 合理 | > 1.2 高估"
        )
        st.metric("AHR999 囤幣指標", f"{ahr_val:.2f}", ahr_state, help=ahr_tooltip)

        mvrv_z    = curr.get('MVRV_Z_Proxy', 0)
        mvrv_state = (
            "🔥 過熱頂部 (>3.0)" if mvrv_z > 3.0
            else ("🟢 價值低估 (<0)" if mvrv_z < 0 else "中性區域")
        )
        st.metric("MVRV Z-Score (Proxy)", f"{mvrv_z:.2f}", mvrv_state)
        st.metric(
            "BTC 生態系 TVL",
            f"${tvl_val / 1e9:.2f}B" if tvl_val > 1e9 else f"${tvl_val:.2f}B",
            "↑ 持續增長" if tvl_val > 0 else "↓ 資金流出",
        )
        etf_flow = proxies['etf_flow']
        st.metric(
            "現貨 ETF 淨流量 (24h)", f"{etf_flow:+.1f}M",
            "↑ 機構買盤" if etf_flow > 0 else "↓ 機構拋壓",
        )
        fr_state = (
            "🔥 多頭過熱" if funding_rate > 0.03
            else ("🟢 情緒中性" if funding_rate > 0 else "❄️ 空頭主導")
        )
        st.metric("資金費率", f"{funding_rate:.4f}%", fr_state,
                  delta_color="inverse" if funding_rate > 0.03 else "normal")

    # ── Level 3: 宏觀視角（v1.1：全面 fallback 備援）─────────────────────────
    with col3:
        st.markdown("### Level 3: 宏觀視角")

        # DXY 相關性
        dxy_is_fb = getattr(dxy, 'is_fallback', False)
        if not dxy.empty and not dxy_is_fb:
            # ▸ tz 標準化（避免 tz-aware vs naive 比較）
            _btc = btc.copy()
            _dxy = dxy.copy()
            if _btc.index.tz is not None:
                _btc.index = _btc.index.tz_localize(None)
            if _dxy.index.tz is not None:
                _dxy.index = _dxy.index.tz_localize(None)
            comm_idx = _btc.index.intersection(_dxy.index)
            if len(comm_idx) >= 90:
                corr_90 = _btc.loc[comm_idx]['close'].rolling(90).corr(
                    _dxy.loc[comm_idx]['close']
                ).iloc[-1]
                if corr_90 != corr_90:
                    st.metric("BTC vs DXY 相關性 (90d)", "計算中", "數據累積不足 90 天")
                else:
                    st.metric(
                        "BTC vs DXY 相關性 (90d)", f"{corr_90:.2f}",
                        "高度負相關 (正常)" if corr_90 < -0.5 else "相關性減弱/脫鉤",
                    )
            else:
                st.metric("BTC vs DXY 相關性 (90d)", "—", "DXY 共同數據不足")
        else:
            fb = _FALLBACK["dxy"]
            fb_note = getattr(dxy, 'fallback_note', f"備援 {fb['date']}")
            st.metric("BTC vs DXY 相關性 (90d)", "—", f"⚠️ {fb_note}")

        # 穩定幣市值
        stab_mcap = getattr(realtime_data, 'stablecoin_mcap', None)
        if stab_mcap is not None and stab_mcap > 0:
            st.metric(
                "全球穩定幣市值",
                f"${stab_mcap:.2f}B",
                "↑ 流動性充沛" if stab_mcap > 100 else "流動性一般",
            )
        else:
            st.metric("全球穩定幣市值", "—", "連線中，稍候重整")

        # M2 貨幣供應量（fallback：靜態值）
        m2_df = fetch_m2_series()
        if not m2_df.empty and not getattr(m2_df, 'is_fallback', False):
            m2_series = m2_df['m2_billions'].reindex(chart_df.index, method='ffill')
            st.line_chart(m2_series, height=120)
            st.caption("美國 M2 貨幣供應量 (FRED WM2NS, 十億美元)")
        elif not m2_df.empty and getattr(m2_df, 'is_fallback', False):
            fb_val  = m2_df['m2_billions'].iloc[-1]
            fb_date = str(m2_df.index[-1].date())
            st.metric("美國 M2 (備援)", f"${fb_val:,.0f}B",
                      f"⚠️ FRED 連線失敗，顯示 {fb_date} 已知值")
        else:
            fb = _FALLBACK["m2"]
            st.metric("美國 M2 (備援)", f"${fb['value']:,.0f}B",
                      f"⚠️ FRED 連線失敗，靜態值 ({fb['date']})")

        st.markdown("---")
        st.markdown("#### 🧠 宏觀數據")
        m_col1, m_col2 = st.columns(2)

        # JPY（fallback：靜態值）
        with m_col1:
            jpy = fetch_usdjpy()
            if jpy.get('rate') is not None:
                fb_badge = " ⚠️(備援)" if jpy.get('is_fallback') else ""
                st.metric(
                    f"🇯🇵 日圓匯率 ({jpy['source']}){fb_badge}",
                    f"¥{jpy['rate']:.2f}",
                    f"{jpy['change_pct']:+.2f}% {jpy['trend']}",
                    delta_color="inverse",
                )
            else:
                fb = _FALLBACK["usdjpy"]
                st.metric(
                    f"🇯🇵 日圓匯率 (備援 {fb['date']})",
                    f"¥{fb['value']:.2f}",
                    "⚠️ Yahoo/FRED 連線失敗，靜態備援值",
                    delta_color="off",
                )

        # CPI（fallback：靜態值）
        with m_col2:
            cpi = fetch_us_cpi_yoy()
            if cpi.get('yoy_pct') is not None:
                fb_badge = " ⚠️(備援)" if cpi.get('is_fallback') else ""
                st.metric(
                    f"🇺🇸 美國 CPI YoY ({cpi['latest_date']}){fb_badge}",
                    f"{cpi['yoy_pct']:.1f}%",
                    cpi['trend'],
                    delta_color="inverse",
                )
            else:
                fb = _FALLBACK["cpi"]
                st.metric(
                    f"🇺🇸 美國 CPI YoY (備援 {fb['date']})",
                    f"{fb['value']:.1f}%",
                    "⚠️ FRED 連線失敗，靜態備援值",
                    delta_color="off",
                )

        # 量子威脅等級
        qt = get_quantum_threat_level()
        st.markdown("---")
        st.metric(
            "⚛️ 量子威脅等級",
            qt['level'],
            qt['status'],
            help=f"{qt['desc']}\n\n預估威脅成熟: {qt['year_est']} ｜ {qt['updated']}",
        )
        st.caption(
            f"距破解 secp256k1 差距約 4 個數量級 ｜ 預估威脅成熟: {qt['year_est']} ｜ "
            f"NIST PQC 2024 已發布 ｜ 關注 OP_CAT 升級"
        )