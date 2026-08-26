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
from datetime import datetime, timezone

from service.macro_data import fetch_m2_series, fetch_usdjpy, fetch_us_cpi_yoy
from core.bear_bottom import (
    calculate_bear_bottom_score,
    calculate_market_cycle_score_breakdown,
    score_series,
)
from core.season_forecast import (
    forecast_price,
    get_cycle_comparison_table,
    get_power_law_forecast,
    CYCLE_HISTORY,
)
from core.bottom_floors import compute_all_bottom_estimates
from core.action_ensemble import compute_composite_action, POSITION_NOTE, CRYPTO_ACTION_NOTES
from core.relative_high import (compute_relative_high, compute_cycle_top_estimates,
                                compute_cycle_top_state, FUNDING_HOT_8H, FUNDING_BASELINE_8H)
from core.relative_low import (compute_relative_low, LOW_LEVEL_STRONG, LOW_LEVEL_VALUE,
                               LOW_LEVEL_COOL, LOW_LEVEL_NEUTRAL, LOW_VETO_VALIDATED)
from core.trend_direction import compute_trend_direction
from service.bottom_metrics import get_latest_bottom_metrics, fetch_hashrate_history_ths
from service.etf_flow import get_etf_flow_summary
from service.market_snapshot import get_oi_stats, get_btcd_trend
from service.macro_data import (
    fetch_us_cpi_yoy, fetch_us_pce_yoy, fetch_nfp, fetch_unrate, get_next_macro_event,
)
from handler.components.macro_utils import (
    _score_meta,
    _bear_score_meta,
    _build_cycle_gauge,
    _build_phase_gauge,
    _season_css_color,
)


def _make_mc_cache_key(chart_df, tvl_hist, stable_hist, fund_hist):
    s = f"{len(chart_df)}_{chart_df.index[-1] if not chart_df.empty else ''}"
    s += f"_{len(tvl_hist)}_{len(stable_hist)}_{len(fund_hist)}"
    return hashlib.md5(s.encode()).hexdigest()


def _make_bb_cache_key(btc):
    s = f"{len(btc)}_{btc.index[-1] if not btc.empty else ''}"
    return hashlib.md5(s.encode()).hexdigest()
_FALLBACK = {'dxy': {'value': 106.5, 'date': '2025-02-21'}, 'm2': {'value': 21450, 'date': '2025-01-01'}, 'cpi': {'value': 3.0, 'date': '2025-01-01'}, 'usdjpy': {'value': 150.5, 'date': '2025-02-21'}}
KNOWN_BOTTOMS = [('2015-08-01', '2015-09-30', '2015 Bear Bottom'), ('2018-11-01', '2019-02-28', '2018-19 Bear Bottom'), ('2020-03-01', '2020-04-30', '2020 COVID Crash'), ('2022-11-01', '2023-01-31', '2022 FTX Bear Bottom')]

def _render_season_timeline(season_info: dict, effective_season: str=None):
    fig = go.Figure()
    season_keys = ['spring', 'summer', 'autumn', 'winter']
    season_colors = ['#1b5e20', '#f9a825', '#e65100', '#0d47a1']
    season_labels = ['🌱 春 (月0-11)', '☀️ 夏 (月12-23)', '🍂 秋 (月24-35)', '❄️ 冬 (月36-47)']
    for i, (key, col, lab) in enumerate(zip(season_keys, season_colors, season_labels)):
        is_eff = effective_season == key and effective_season != season_info['season']
        fig.add_shape(type='rect', x0=i * 12, x1=(i + 1) * 12, y0=0, y1=1, fillcolor=col, opacity=0.7 if is_eff else 0.35, layer='below', line=dict(color='#ffffff', width=3) if is_eff else dict(width=0))
        fig.add_annotation(x=i * 12 + 6, y=0.5, text=lab + (' ← 實際' if is_eff else ''), showarrow=False, font=dict(size=11, color='white'))
    m = season_info['month_in_cycle']
    fig.add_shape(type='line', x0=m, x1=m, y0=0, y1=1, line=dict(color='#ffffff', width=3))
    fig.add_annotation(x=m, y=1.1, text=f'現在 (月{m})', showarrow=False, font=dict(size=12, color='white'))
    fig.update_layout(height=130, margin=dict(l=10, r=10, t=35, b=10), template='plotly_dark', xaxis=dict(range=[0, 48], showticklabels=False, showgrid=False, zeroline=False), yaxis=dict(range=[0, 1.25], showticklabels=False, showgrid=False, zeroline=False), paper_bgcolor='#0e1117', plot_bgcolor='#0e1117')
    return fig

def _render_forecast_chart(btc: pd.DataFrame, fc: dict):
    hist_2y = btc.tail(365 * 2)
    future_pl = get_power_law_forecast(btc, months_ahead=12)
    # B1（2026-07-06）消費端盤點：SEASON_ENGINE="v2" 時 forecast_type 可能是 'observe'
    # （target_median/low/high 皆 None——四季論刻意不出目標價，見 season_v2_design.md）。
    # 目前預設 SEASON_ENGINE="v1" 永不產生 'observe'，本守門為未來切換 v2 預先鋪路。
    if fc.get('target_median') is None:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hist_2y.index, y=hist_2y['close'], mode='lines',
                                 name='BTC 歷史收盤', line=dict(color='#ffffff', width=2)))
        fig.update_layout(height=500, template='plotly_dark', yaxis_type='log',
                          title=dict(text='🔭 觀察期（v2 observe）— 轉折未確認，暫不出目標價', font=dict(size=16)),
                          paper_bgcolor='#0e1117')
        return fig
    is_bull = fc['forecast_type'] == 'bull_peak'
    ribbon_color = 'rgba(255,235,59,0.18)' if is_bull else 'rgba(66,165,245,0.18)'
    median_color = '#ffeb3b' if is_bull else '#42a5f5'
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(future_pl.index) + list(future_pl.index[::-1]), y=list(future_pl['upper']) + list(future_pl['lower'][::-1]), fill='toself', fillcolor='rgba(255,204,0,0.07)', line=dict(color='rgba(0,0,0,0)'), name='冪律走廊'))
    fig.add_trace(go.Scatter(x=future_pl.index, y=future_pl['median'], mode='lines', line=dict(color='#ffcc00', width=1, dash='dot'), name='冪律中線'))
    fig.add_trace(go.Scatter(x=hist_2y.index, y=hist_2y['close'], mode='lines', name='BTC 歷史收盤', line=dict(color='#ffffff', width=2)))
    est_date = fc['estimated_date']
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    ribbon_x = [today, est_date, est_date, today]
    ribbon_y = [fc['target_high']] * 2 + [fc['target_low']] * 2
    fig.add_trace(go.Scatter(x=ribbon_x + [today], y=ribbon_y + [fc['target_high']], fill='toself', fillcolor=ribbon_color, line=dict(color='rgba(0,0,0,0)'), name='目標價區間'))
    fig.add_shape(type='line', x0=today, x1=est_date, y0=fc['target_median'], y1=fc['target_median'], line=dict(color=median_color, width=2.5, dash='dash'))
    label = '🎯 牛市目標高點' if is_bull else '🎯 熊市目標低點'
    fig.add_annotation(x=est_date, y=fc['target_median'], text=f'{label}<br>${fc['target_median']:,.0f}', showarrow=True, arrowhead=2, font=dict(color=median_color, size=12), bgcolor='#1e1e1e', bordercolor=median_color, borderwidth=1)
    for val, clr, lbl in [(fc['target_high'], '#ff9800', '樂觀目標'), (fc['target_low'], '#78909c', '保守目標')]:
        fig.add_shape(type='line', x0=today, x1=est_date, y0=val, y1=val, line=dict(color=clr, width=1.2, dash='dot'))
        fig.add_annotation(x=est_date, y=val, text=f'{lbl}: ${val:,.0f}', showarrow=False, xanchor='left', font=dict(color=clr, size=10))
    fig.add_shape(type='line', x0=today, x1=today, y0=0, y1=1, xref='x', yref='paper', line=dict(color='#888888', width=1, dash='dash'))
    fig.update_layout(height=500, template='plotly_dark', yaxis_type='log', title=dict(text=f'{('📈 牛市最高價' if is_bull else '📉 熊市最低價')} 預測 — 未來 12 個月', font=dict(size=16)), legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='right', x=1), paper_bgcolor='#0e1117')
    return fig

def _render_cycle_waterfall(fc: dict):
    labels, values, colors, bar_texts = ([], [], [], [])
    for i, c in enumerate(CYCLE_HISTORY):
        yr = c['halving'].year
        if c['is_complete']:
            labels.append(f'第{i + 1}週期\n({yr})')
            values.append(c['peak_mult'])
            colors.append('#ff9800')
            bar_texts.append(f'{c['peak_mult']:.1f}x')
        else:
            # 進行中：用 forecast 的實算 cycle_ath 重算倍數（市場可能已創新高）
            live_ath = (fc.get('market_state', {}) or {}).get('cycle_ath', 0)
            live_mult = max(c['peak_mult'], live_ath / c['halving_price']) if c['halving_price'] else c['peak_mult']
            labels.append(f'第{i + 1}週期\n({yr}) 進行中')
            values.append(live_mult)
            colors.append('#42a5f5')
            bar_texts.append(f'{live_mult:.2f}x ✓\n(ATH已達)')
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color=colors, text=bar_texts, textposition='outside'))
    fig.add_trace(go.Scatter(x=labels, y=values, mode='lines+markers', line=dict(color='#ffffff', width=1.5, dash='dot'), showlegend=False))
    fig.update_layout(height=320, template='plotly_dark', title='歷史牛市漲幅遞減規律（相對減半時價格）', yaxis_title='倍數 (x)', paper_bgcolor='#0e1117', showlegend=False, annotations=[dict(text='🔵 進行中 = ATH倍數已確認，熊市底部尚未完成', xref='paper', yref='paper', x=0, y=-0.15, showarrow=False, font=dict(size=10, color='#42a5f5'), align='left')])
    return fig

@st.cache_data(ttl=1800, show_spinner=False)
def _gather_radar_externals(cache_key: str) -> dict:
    """
    相對高/低點雷達共用的外部資料（ETF 流量 / SOPR / BTC.D 趨勢 / 總經）一次抓齊並
    快取 30 分鐘，避免 Streamlit 每次 rerun 重打。各服務內部另有 json 快取，此層再省一次。
    回傳純 dict（可序列化），失敗欄位為 None。

    macro 同時帶逃頂（hot/strong = 逆風）與抄底（cool/weak = 順風）兩套布林欄位，
    兩側雷達各取所需：逃頂吃 cpi_hot/jobs_strong，抄底吃 cpi_cool/jobs_weak。
    """
    out = {"etf": None, "sopr": None, "btcd": None, "macro": None, "asof": None, "mvrv_z": None}
    try:
        out["etf"] = get_etf_flow_summary()
    except Exception:
        pass
    try:
        bm = get_latest_bottom_metrics()
        out["sopr"] = bm.get("sopr")
        out["asof"] = bm.get("asof")
        out["mvrv_z"] = bm.get("mvrv_zscore")   # 2026-07 已驗證計入 onchain 子分，見 core/relative_high.py
    except Exception:
        pass
    try:
        out["btcd"] = get_btcd_trend()
    except Exception:
        pass
    try:
        cpi = fetch_us_cpi_yoy(); pce = fetch_us_pce_yoy()
        nfp = fetch_nfp(); unrate = fetch_unrate()
        nfp_k = nfp.get("change_k") or 0
        unrate_pp = unrate.get("change_pp") or 0
        jobs_strong = (nfp_k > 150) or (unrate_pp < 0)
        jobs_weak   = (nfp_k < 50) or (unrate_pp > 0)   # 新增就業疲弱或失業上升 = 降息傾向
        cpi_trend = cpi.get("trend") or ""; pce_trend = pce.get("trend") or ""
        nxt = get_next_macro_event()
        out["macro"] = {
            # 逃頂側（過熱 = 逆風）
            "cpi_hot": "升溫" in cpi_trend,
            "pce_hot": "升溫" in pce_trend,
            "jobs_strong": bool(jobs_strong),
            # 抄底側（降溫 = 順風）
            "cpi_cool": "降溫" in cpi_trend,
            "pce_cool": "降溫" in pce_trend,
            "jobs_weak": bool(jobs_weak),
            # 共用
            "event_within_days": nxt.get("days"),   # db/macro_events.json（Notion 同源）
            "next_event_type": nxt.get("type"),
        }
    except Exception:
        pass
    return out


def _warn_unavailable(name: str, e: Exception) -> None:
    """核心區塊計算失敗時的統一警示（保留例外型別＋訊息，方便回報）。"""
    st.warning(f'⚠️ {name}暫不可用（{type(e).__name__}: {e}）')


def _render_trend_banner(btc, curr):
    """🧭 趨勢方向橫幅 — 波段雷達第三軸（風往哪吹）。與 LINE 推播、BTC_WATCH 同源 core/trend_direction。"""
    st.markdown('##### 🧭 目前趨勢方向（日線）')
    try:
        td = compute_trend_direction(curr, btc)   # curr=row, btc=df（與逃頂/抄底同參數）
    except Exception as e:
        _warn_unavailable('趨勢方向', e)
        return

    net = td['trend_score']
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=net,
        number={'suffix': ' 分', 'font': {'size': 26}},
        gauge={
            'axis': {'range': [-100, 100], 'tickvals': [-100, -50, -20, 20, 50, 100]},
            'bar': {'color': td['trend_color'], 'thickness': 0.28},
            'steps': [
                {'range': [-100, -50], 'color': '#3a1518'},
                {'range': [-50, -20], 'color': '#3a2415'},
                {'range': [-20, 20], 'color': '#2a2a2a'},
                {'range': [20, 50], 'color': '#15321f'},
                {'range': [50, 100], 'color': '#0f3d2a'},
            ],
            'threshold': {'line': {'color': 'white', 'width': 3}, 'thickness': 0.85, 'value': net},
        },
        title={'text': f"<b>{td['trend_level']}</b><br>"
                       f"<span style='font-size:0.8em;color:gray'>Trend Direction（多頭+／空頭−）</span>"},
    ))
    fig.update_layout(height=240, margin=dict(t=70, b=10, l=30, r=30))
    st.plotly_chart(fig, use_container_width=True, key='trend_dir_gauge')
    st.caption(f"操作意涵：{td['trend_action']}")

    sig = td['trend_signals']
    cols = st.columns(4)
    _dim_zh = {'ma_structure': '均線結構', 'macd': 'MACD 動能',
               'slope': '斜率動能', 'adx': 'ADX 確信'}
    for col, key in zip(cols, ['ma_structure', 'macd', 'slope', 'adx']):
        d = sig[key]
        col.metric(f"{_dim_zh[key]} ({d['score']:+d}/±{d['max']})", d['value'])
        col.caption(d['label'])
    return td


@st.cache_data(ttl=3600, show_spinner=False)
def _radar_funding_8h():
    """計分用的日均資費（與校準同口徑）。取不到回 None → 呼叫端沿用即時值。
    2026-08-25 獨立檢核 🟠 No.6：BTC_WATCH 改吃日均後，dashboard/LINE 仍吃即時單筆，
    反而新增一處三方分歧（實測 344/2542 日、最大差 10 分）→ 三方一起改。"""
    try:
        from service.funding_history import funding_8h_daily_mean
        return funding_8h_daily_mean()
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def _radar_metric_hist():
    """SOPR / F&G 歷史（抄底 PiT 分位子項用）。抓不到回 {} → 退回絕對階梯。"""
    try:
        from service.metric_history import sopr_hist, fng_hist
        return {'sopr': sopr_hist() or None, 'fng': fng_hist() or None}
    except Exception:
        return {}


@st.cache_data(ttl=3600, show_spinner=False)
def _radar_funding_hist():
    """
    抄底負費率子項的 PiT 滾動分位所需歷史（2026-08-25 補）。
    背景：改動當初只有 BTC_WATCH 餵歷史，dashboard 與 LINE 推播仍跑純絕對階梯
    → 新環境 623 日中有 232 日（37.2%）三方分數不同、最大差 7 分（獨立檢核 🟠 No.2）。
    抓不到回 None → core 端自動退回絕對階梯，不讓面板掛掉。
    """
    try:
        from service.funding_history import funding_ann_hist
        return funding_ann_hist(window=400) or None
    except Exception:
        return None


def _render_escape_block(btc, curr, funding_rate, fng_val, realtime_data):
    """逃頂評分段（過熱該止盈）— 波段雷達上半。與 LINE 推播、BTC_WATCH 同源 core/relative_high。"""
    st.markdown('##### 🚨 逃頂評分（過熱該止盈）')
    try:
        price = float(curr['close'])
        oi_total = getattr(realtime_data, 'open_interest', None)
        oi_stats = get_oi_stats(oi_total) if oi_total else None
        ext = _gather_radar_externals(str(btc.index[-1]))
        rh = compute_relative_high(
            price, btc.iloc[-1], btc,
            funding_8h=funding_rate,
            funding_ann_hist=_radar_funding_hist(),
            oi_stats=oi_stats,
            etf_summary=ext.get('etf'),
            sopr=ext.get('sopr'),
            fng=float(fng_val) if fng_val is not None else None,
            btc_d_trend=ext.get('btcd'),
            macro=ext.get('macro'),
            mvrv_z=ext.get('mvrv_z'),
        )
    except Exception as e:
        _warn_unavailable('逃頂評分', e)
        return

    score = rh['escape_score']
    level, color = rh['escape_level'], rh['escape_color']
    sig = rh['escape_signals']

    st.caption('五維加權 0–100（合約過熱＋技術衰竭＋鏈上派發＋情緒過熱＋總經逆風），分數越高越過熱。')
    gauge = go.Figure(go.Indicator(
        mode='gauge+number', value=score,
        title={'text': "波段逃頂評分<br><span style='font-size:0.8em;color:gray'>Swing Escape-Top</span>",
               'font': {'size': 16}},
        gauge={'axis': {'range': [0, 100]}, 'bar': {'color': color}, 'bgcolor': '#1e1e1e',
               'borderwidth': 2, 'bordercolor': '#333',
               'steps': [{'range': [0, 25], 'color': '#1a3a1a'}, {'range': [25, 45], 'color': '#2a2a2a'},
                         {'range': [45, 60], 'color': '#3a3a1a'}, {'range': [60, 75], 'color': '#3a2a1a'},
                         {'range': [75, 100], 'color': '#3a1a1a'}],
               'threshold': {'line': {'color': '#fff', 'width': 3}, 'thickness': 0.75, 'value': score}}))
    gauge.update_layout(height=260, template='plotly_dark', paper_bgcolor='#0e1117', font={'color': 'white'})
    eg1, eg2 = st.columns([1, 1])
    with eg1:
        st.plotly_chart(gauge, use_container_width=True)
    with eg2:
        st.markdown(f'### {level}')
        st.markdown(f'**評分: {score}/100**')
        st.info(f'📋 **操作建議**: {rh["escape_action"]}')
        st.markdown("""
        | 分數 | 狀態 | 行動 |
        |------|------|------|
        | 75-100 | 強烈逃頂 | 分批止盈/對沖 |
        | 60-75 | 明確過熱 | 減倉、收緊止盈 |
        | 45-60 | 偏熱警戒 | 停止加倉 |
        | 25-45 | 中性 | 正常持有 |
        | 0-25 | 無過熱 | — |
        """)

    # 五維卡片
    dim_names = {'derivatives': '① 合約過熱', 'technical': '② 技術衰竭', 'onchain': '③ 鏈上派發',
                 'sentiment': '④ 情緒過熱', 'macro': '⑤ 總經逆風'}
    unfitted = set(rh.get('unfitted_dims', []))
    cols = st.columns(5)
    accumulating = []
    for col, (key, name) in zip(cols, dim_names.items()):
        s = sig[key]
        bar_pct = (s['score'] / s['max'] * 100) if s['max'] else 0
        tag = " <span style='color:#ff9800;font-size:0.7rem;'>(未擬合)</span>" if key in unfitted else ""
        if '累積中' in s['label'] or '無資料' in s['label']:
            accumulating.append(name.split(' ')[-1] if ' ' in name else name)
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{name}{tag}</div>
            <div class="metric-value" style="font-size:1.1rem;">{s['value']}</div>
            <div class="metric-delta" style="font-size:0.72rem;">{s['label']}</div>
            <div style="background:#333;border-radius:4px;height:6px;margin-top:8px;">
                <div style="background:{color};width:{bar_pct:.0f}%;height:6px;border-radius:4px;"></div>
            </div>
            <div style="color:#888;font-size:0.72rem;text-align:right;">{s['score']}/{s['max']} 分</div>
        </div>""", unsafe_allow_html=True)
    if accumulating:
        st.caption('ℹ️ 部分維度為 0 是「資料尚未到位」而非「無訊號」：'
                   + '、'.join(accumulating)
                   + '。OI/BTC.D 由本地每日快照自建歷史（剛起步，需數週累積）；'
                     'SOPR/總經視來源可達性（FRED 在公司網路常被擋，雲端正常）。'
                     '其餘為 0 則代表市場當下確實不過熱。')
    return rh


def _render_dip_block(btc, curr, funding_rate, fng_val, realtime_data):
    """抄底評分段（低估可進場）— 波段雷達下半。與 LINE 推播、BTC_WATCH 同源 core/relative_low。"""
    st.markdown('##### 🟢 抄底評分（低估可進場）')
    try:
        price = float(curr['close'])
        oi_total = getattr(realtime_data, 'open_interest', None)
        oi_stats = get_oi_stats(oi_total) if oi_total else None
        ext = _gather_radar_externals(str(btc.index[-1]))
        rl = compute_relative_low(
            price, btc.iloc[-1], btc,
            funding_8h=(_radar_funding_8h() if _radar_funding_8h() is not None else funding_rate),
            funding_ann_hist=_radar_funding_hist(),
            sopr_hist=_radar_metric_hist().get('sopr'),
            fng_hist=_radar_metric_hist().get('fng'),
            oi_stats=oi_stats,
            etf_summary=ext.get('etf'),
            sopr=ext.get('sopr'),
            fng=float(fng_val) if fng_val is not None else None,
            btc_d_trend=ext.get('btcd'),
            macro=ext.get('macro'),   # 同時含 cool/weak 欄位，抄底側取順風項
            mvrv_z=ext.get('mvrv_z'),
        )
    except Exception as e:
        _warn_unavailable('抄底評分', e)
        return

    score = rl['low_score']
    level, color = rl['low_level'], rl['low_color']
    sig = rl['low_signals']

    st.caption('六維加權 0–100（長週期深跌＋合約超冷＋技術回穩＋情緒恐慌＋鏈上吸籌＋總經順風），分數越高越低估。')
    gauge = go.Figure(go.Indicator(
        mode='gauge+number', value=score,
        title={'text': "波段抄底評分<br><span style='font-size:0.8em;color:gray'>Swing Dip-Buy</span>",
               'font': {'size': 16}},
        gauge={'axis': {'range': [0, 100]}, 'bar': {'color': color}, 'bgcolor': '#1e1e1e',
               'borderwidth': 2, 'bordercolor': '#333',
               'steps': [{'range': [0, 25], 'color': '#3a1a1a'}, {'range': [25, 45], 'color': '#2a2a2a'},
                         {'range': [45, 60], 'color': '#3a3a1a'}, {'range': [60, 75], 'color': '#1a3a1a'},
                         {'range': [75, 100], 'color': '#0a3a2a'}],
               'threshold': {'line': {'color': '#fff', 'width': 3}, 'thickness': 0.75, 'value': score}}))
    gauge.update_layout(height=260, template='plotly_dark', paper_bgcolor='#0e1117', font={'color': 'white'})
    dg1, dg2 = st.columns([1, 1])
    with dg1:
        st.plotly_chart(gauge, use_container_width=True)
    with dg2:
        st.markdown(f'### {level}')
        st.markdown(f'**評分: {score}/100**')
        st.success(f'📋 **操作建議**: {rl["low_action"]}')
        # 門檻一律由 core.relative_low 的常數生成，**不要再寫死數字**——
        # 這張表曾停留在 2026-08-25 重校前的 75/60/45/25，與實際分級差了整整一級。
        st.markdown(f"""
        | 分數 | 狀態 | 行動 |
        |------|------|------|
        | {LOW_LEVEL_STRONG}-100 | 強力抄底訊號 | 分批進場/回補空單（需配合動態地板確認） |
        | {LOW_LEVEL_VALUE}-{LOW_LEVEL_STRONG} | 明確低估 | 可開始定投/減空 |
        | {LOW_LEVEL_COOL}-{LOW_LEVEL_VALUE} | 偏冷觀察 | 留意打底，勿純憑超賣搶反彈 |
        | {LOW_LEVEL_NEUTRAL}-{LOW_LEVEL_COOL} | 中性 | 正常持有 |
        | {LOW_VETO_VALIDATED + 1}-{LOW_LEVEL_NEUTRAL} | 無底部訊號 | 勿接刀〔**此段未經驗證**〕 |
        | 0-{LOW_VETO_VALIDATED} | ⛔ **實證否決區** | 不進場（其後180日中位 +1.3% vs 其他日 +27.5%，p=2e-07；非 BTC 幣對 4/4 獨立驗收通過） |
        """)

    # 六維卡片
    dim_names = {'cycle': '① 長週期深跌', 'derivatives': '② 合約超冷', 'technical': '③ 技術回穩',
                 'sentiment': '④ 情緒恐慌', 'onchain': '⑤ 鏈上吸籌', 'macro': '⑥ 總經順風'}
    unfitted = set(rl.get('unfitted_dims', []))
    rule_based = set(rl.get('rule_based_dims', []))   # 規則式維度（如 macro 事件臨近）不可統計擬合
    cols = st.columns(6)
    accumulating = []
    for col, (key, name) in zip(cols, dim_names.items()):
        s = sig[key]
        bar_pct = (s['score'] / s['max'] * 100) if s['max'] else 0
        if key in unfitted:
            tag = " <span style='color:#ff9800;font-size:0.7rem;'>(未擬合)</span>"
        elif key in rule_based:
            tag = " <span style='color:#5dade2;font-size:0.7rem;'>(規則式)</span>"
        else:
            tag = ""
        if '累積中' in s['label'] or '無資料' in s['label']:
            accumulating.append(name.split(' ')[-1] if ' ' in name else name)
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{name}{tag}</div>
            <div class="metric-value" style="font-size:1.0rem;">{s['value']}</div>
            <div class="metric-delta" style="font-size:0.7rem;">{s['label']}</div>
            <div style="background:#333;border-radius:4px;height:6px;margin-top:8px;">
                <div style="background:{color};width:{bar_pct:.0f}%;height:6px;border-radius:4px;"></div>
            </div>
            <div style="color:#888;font-size:0.72rem;text-align:right;">{s['score']}/{s['max']} 分</div>
        </div>""", unsafe_allow_html=True)
    if accumulating:
        st.caption('ℹ️ 部分維度為 0 是「資料尚未到位」而非「無訊號」：'
                   + '、'.join(accumulating)
                   + '。Mayer/200週需數百日歷史；ETF 連續流入＆SOPR 視來源可達性'
                     '（bitcoin-data.com 在公司網路偶限流，雲端正常）。其餘為 0 則代表市場當下確實不夠低估。')
    return rl


def _render_composite_action(td, rh, rl):
    """🎯 三軸合成行動建議 — 與 LINE 推播同源 core/action_ensemble。任一軸缺失則隱藏。"""
    comp = compute_composite_action(
        (td or {}).get('trend_score'),
        (rh or {}).get('escape_score'),
        (rl or {}).get('low_score'),
        notes=CRYPTO_ACTION_NOTES,
    )
    if comp is None:
        return
    st.markdown(f"""
    <div style="background:#1e2530;border-left:4px solid {comp['color']};border-radius:6px;
                padding:12px 16px;margin-top:8px;">
        <span style="font-size:1.05rem;font-weight:bold;color:{comp['color']};">
            {comp['emoji']} 三軸合成．今日行動：{comp['action']}（{comp['pos_label']}）
        </span><br>
        <span style="color:#bbb;font-size:0.85rem;">{comp['detail']}</span>
    </div>""", unsafe_allow_html=True)
    if comp.get("confidence_note"):
        st.caption(comp["confidence_note"])
    st.caption(f"⚠️ {POSITION_NOTE}；三軸 = 趨勢方向 × 逃頂 × 抄底，與 LINE 推播同源 core/action_ensemble。")


# 完整評分標準（與 core/relative_high、core/relative_low 的閾值同步——改 core 閾值時一併更新此表）
_ESCAPE_RUBRIC_MD = """
**🚨 逃頂評分 · 五維計分標準**（滿分 100，分數越高越過熱；命中較高門檻者取該檔分數）

| 維度（滿分） | 子項 | 計分門檻 |
|---|---|---|
| ① 合約過熱 **30** | 資金費率年化 (20) | ≥50%→20｜≥40%→17｜≥30%→14｜≥20%→6｜≥12%→2｜<12%→0｜負費率→0 |
| | OI 分位 (10) | 近期新高 / ≥95 分位→10｜≥85→7｜≥70→4｜其餘→0 |
| ② 技術衰竭 **25** | 頂背離 (18) | RSI+MACD 雙頂背離→18｜單指標背離→8~12｜無→0 |
| | RSI_14 超買 (7) | ≥80→7｜≥75→5｜≥70→3｜其餘→0 |
| ③ 鏈上派發 **20**〔未擬合〕 | ETF 連續流出 (12) | ≥10日→12｜≥7日→10｜≥5日→7｜≥3日→4｜1–2日→2｜淨流入→0 |
| | SOPR (8) | ≥1.08→8｜≥1.05→6｜≥1.03→4｜≥1.01→2｜其餘→0 |
| ④ 情緒過熱 **15** | F&G 貪婪 (10) | ≥90→10｜≥80→8｜≥75→5｜≥70→3｜其餘→0 |
| | BTC.D 下降 (5) | 下降 / ≤−1.0pp→5｜≤−0.5pp→3｜其餘→0 |
| ⑤ 總經逆風 **10** | 通膨/就業 hawkish (7) | 通膨升溫 +4、就業強勁 +3（上限 7） |
| | 事件臨近 (3) | ≤1日→3｜≤3日→2｜≤7日→1｜無→0 |

等級：≥75 強烈逃頂｜≥60 明確過熱（觸發 LINE 逃頂警報）｜≥45 偏熱警戒｜≥25 中性｜<25 無過熱
"""

_LOW_RUBRIC_MD = """
**🟢 抄底評分 · 六維計分標準**（滿分 100，分數越高越低估；命中較低門檻者取該檔分數）

| 維度（滿分） | 子項 | 計分門檻 |
|---|---|---|
| ① 長週期深跌 **25**（最強維度）| Mayer 倍數 (10) | <0.8→10｜<1.0→6｜<1.2→3｜≥1.2→0 |
| | 200週均線比 (9) | <1.0→9｜<1.3→6｜<2.0→3｜≥2.0→0 |
| | 冪律比 (6) | <2.0→6｜<5.0→3｜≥5.0→0 |
| ② 合約超冷 **20**〔負費率已校·OI未擬合〕 | 負費率年化 (10) | ≤−20%→10｜≤−10%→8｜≤−5%→6｜≤−2%→3｜<0→1｜≥0→0 |
| | OI 1h 清洗 (10) | ≤−8%→10｜≤−5%→7｜≤−3%→4｜其餘→0 |
| ③ 技術回穩 **20** | 底背離 (14) | RSI+MACD 雙底背離→14｜單指標背離→6~10｜無→0 |
| | RSI_14 超賣 (6) | ≤20→6｜≤25→4｜≤30→2｜其餘→0 |
| ④ 情緒恐慌 **15** | F&G 恐懼 (10) | ≤10→10｜≤20→8｜≤25→5｜≤30→3｜其餘→0 |
| | BTC.D 上升 (5) | 上升 / ≥1.0pp→5｜≥0.5pp→3｜其餘→0 |
| ⑤ 鏈上吸籌 **10**〔SOPR已驗·ETF灰燈〕 | ETF 連續流入 (6) | ≥7日→6｜≥5日→4｜≥3日→2｜其餘→0 |
| | SOPR 割肉 (4) | ≤0.92→4｜≤0.95→3｜≤0.98→2｜其餘→0 |
| ⑥ 總經順風 **10**〔規則式＋待FRED〕 | 通膨/就業 dovish (7)〔待FRED回補驗證〕 | 通膨降溫 +4、就業轉弱 +3（上限 7） |
| | 事件臨近 (3)〔規則式·不可擬合〕 | ≤1日→3｜≤3日→2｜≤7日→1｜無→0 |

等級（2026-08-25 重校、2026-08-26 新增最低級；門檻正本＝`core.relative_low` 的具名常數）：
≥56 強力抄底｜≥54 明確低估（觸發 LINE 抄底訊號）｜≥45 偏冷觀察｜≥26 中性｜6~25 無底部訊號〔未驗證〕｜**≤5 ⛔ 實證否決區**

〔未擬合〕＝權重採專家設定、歷史樣本不足「待累積後可回測」（如 OI 自建快照）；〔灰燈〕＝資料源在純幣安環境可能缺漏。
〔SOPR已驗〕＝SOPR 子項 2026-06 敏感度驗證通過（方向正確、加入無害；ETF 子項 2024+ 資料薄沿用專家權重）。
〔規則式〕＝event-window 事件臨近本質為規則式風險旗標、**永久不可統計擬合**，由規則正確性背書（非權重存疑）。
〔待FRED〕＝dovish flags（通膨/就業）可擬合，但無歷史源（FRED 公司網路被擋）→ 待雲端/家用網路回補後 backtest。
"""


def _render_swing_radar(btc, curr, funding_rate, fng_val, realtime_data):
    """📍 波段雷達 — 短中期雙向相對評分（逃頂＋抄底）。取代舊 C5/C6，與 LINE 推播同源。"""
    st.markdown('---')
    st.subheader('📍 波段雷達 (Swing Radar)')
    st.caption('短中期擇時的**雙向量表**：逃頂（過熱該止盈）＋ 抄底（低估可進場）。兩側天生非對稱——'
               '逃頂靠「合約過熱」、抄底靠「長週期深跌」。≥60 觸發對應 LINE 警報；為風險/估值量表，非精準擇時工具。')
    td = _render_trend_banner(btc, curr)
    st.markdown('')
    # 逃頂/抄底改分頁呈現（降低首屏滾動負擔）；兩側仍各自完整計算，三軸合成在分頁外恆顯
    esc_tab, dip_tab = st.tabs(['🚨 逃頂評分', '🟢 抄底評分'])
    with esc_tab:
        rh = _render_escape_block(btc, curr, funding_rate, fng_val, realtime_data)
    with dip_tab:
        rl = _render_dip_block(btc, curr, funding_rate, fng_val, realtime_data)
    _render_composite_action(td, rh, rl)
    with st.expander('📖 完整評分標準與計分方式（逃頂五維 ＋ 抄底六維，每一檔門檻）', expanded=False):
        st.markdown(_ESCAPE_RUBRIC_MD)
        st.markdown(_LOW_RUBRIC_MD)


_SEASON_ZH_FULL = {'spring': '🌱 春季（復甦）', 'summer': '☀️ 夏季（主升）',
                   'autumn': '🍂 秋季（泡沫破裂）', 'winter': '❄️ 冬季（築底）'}


def _render_season_radar(btc, fc, be, price):
    """🗓️ 四季雷達 — 本輪減半週期的頂底定位（週期頂錨 ＋ 四季論底 ＋ 通道位置 ＋ 牛頂/熊底分）。
    整合 core/relative_high 的高點錨與牛頂分、core/bottom_floors 的最終最低價（即下方 D2）。"""
    try:
        tops = compute_cycle_top_estimates(price, btc)
        cyc = compute_cycle_top_state(btc.iloc[-1], btc, price)
    except Exception as e:
        _warn_unavailable('四季雷達', e)
        return

    top_vals = sorted(e['value'] for e in tops) if tops else []
    top_repr = top_vals[len(top_vals) // 2] if top_vals else None     # 高點錨中位數
    bottom_repr = (be or {}).get('final_low')
    bull = cyc.get('bull_total', 0)
    bear = cyc.get('bear_total', 0)
    eff = cyc.get('effective_season')
    season_label = _SEASON_ZH_FULL.get(eff, eff or '—')

    st.markdown('#### 🗓️ 四季雷達 · 本輪頂底定位')
    st.caption('把「現價」放進本輪減半週期的頂↔底通道：頂取 Pi Cycle／Mayer／冪律／四季論牛頂的**中位錨**，'
               '底取四季論趨勢底（即下方 D2 的最終最低價）。牛頂分／熊底分來自 8 大鏈上技術指標。')

    cT, cB = st.columns(2)
    with cT:
        if top_repr:
            d = (top_repr / price - 1) * 100
            st.markdown(f"""<div style="background:#1a1a2e;border:2px solid #ef5350;border-radius:10px;padding:14px;text-align:center;">
                <div style="color:#888;font-size:0.8rem;">週期頂（高點錨中位）</div>
                <div style="color:#ef5350;font-size:1.8rem;font-weight:800;">${top_repr:,.0f}</div>
                <div style="color:#66bb6a;font-size:0.78rem;">距頂 +{d:.0f}%</div>
            </div>""", unsafe_allow_html=True)
        else:
            st.caption('週期頂錨累積中（需 SMA350/730 等長均線）')
    with cB:
        if bottom_repr:
            d = (price / bottom_repr - 1) * 100
            st.markdown(f"""<div style="background:#1a1a2e;border:2px solid #42a5f5;border-radius:10px;padding:14px;text-align:center;">
                <div style="color:#888;font-size:0.8rem;">四季論底（D2 最終最低價）</div>
                <div style="color:#42a5f5;font-size:1.8rem;font-weight:800;">${bottom_repr:,.0f}</div>
                <div style="color:{'#66bb6a' if price >= bottom_repr else '#ef5350'};font-size:0.78rem;">距底 {d:+.0f}%</div>
            </div>""", unsafe_allow_html=True)
        else:
            st.caption('四季論底暫不可用（見 D2）')

    # 通道位置條（現價在 底↔頂 區間的相對位置）
    if top_repr and bottom_repr and top_repr > bottom_repr:
        pos = max(0.0, min(1.0, (price - bottom_repr) / (top_repr - bottom_repr)))
        pos_pct = pos * 100
        st.markdown(f"""<div style="position:relative;height:38px;margin:14px 0 2px 0;">
            <div style="position:absolute;top:16px;left:0;right:0;height:8px;border-radius:4px;
                 background:linear-gradient(90deg,#42a5f5 0%,#9e9e9e 50%,#ef5350 100%);"></div>
            <div style="position:absolute;top:2px;left:{pos_pct:.1f}%;transform:translateX(-50%);
                 color:#fff;font-size:0.9rem;">▼</div>
            <div style="position:absolute;top:26px;left:{pos_pct:.1f}%;transform:translateX(-50%);
                 color:#fff;font-size:0.7rem;white-space:nowrap;">現價 ${price:,.0f}</div>
            <div style="position:absolute;top:26px;left:0;color:#42a5f5;font-size:0.7rem;">底</div>
            <div style="position:absolute;top:26px;right:0;color:#ef5350;font-size:0.7rem;">頂</div>
        </div>""", unsafe_allow_html=True)
        st.caption(f'現價落在本輪通道 **{pos_pct:.0f}%** 位置（0%＝貼四季論底，100%＝抵高點錨中位）')

    # 週期頂錨明細（頂部價格依據，對稱下方 D2 的底部明細）
    if tops:
        _trs = ''
        for t in tops:
            d = (t['value'] / price - 1) * 100
            _trs += (f"<tr><td style='padding:3px 8px;color:#ffd54f;'>{t['label']}</td>"
                     f"<td style='padding:3px 8px;text-align:right;color:#fff;font-weight:600;'>${t['value']:,.0f}</td>"
                     f"<td style='padding:3px 8px;text-align:right;color:#66bb6a;'>+{d:.0f}%</td>"
                     f"<td style='padding:3px 8px;color:#777;font-size:0.72rem;'>{t['note']}</td></tr>")
        st.markdown(f"""<table style="width:100%;border-collapse:collapse;font-size:0.8rem;margin-top:8px;">
            <tr style="color:#aaa;border-bottom:1px solid #444;">
              <th style="text-align:left;padding:3px 8px;">週期頂錨（依據）</th><th style="text-align:right;padding:3px 8px;">價位</th>
              <th style="text-align:right;padding:3px 8px;">距現價</th><th style="text-align:left;padding:3px 8px;">說明</th></tr>
            {_trs}</table>""", unsafe_allow_html=True)
        st.caption('「週期頂」取上述各錨的**中位數**為代表；底部完整 10 項見下方 D2。')

    # 牛頂分 / 熊底分 + 季節定位句
    mcol1, mcol2, mcol3 = st.columns([1, 1, 2])
    mcol1.metric('牛頂分數', f'{bull}/100', help='8 大鏈上技術指標的牛頂側合計，越高越接近整輪大頂')
    mcol2.metric('熊底分數', f'{bear}/100', help='8 大指標的熊底側合計，越高越接近整輪大底')
    with mcol3:
        if eff == 'autumn':
            _pos = '🍂 高點已過、底部未至 → 逐步減倉、轉穩定資產'
        elif bull >= 60:
            _pos = '🔥 接近整輪大頂 → 分批止盈、收緊移動止盈'
        elif eff == 'winter' or bear >= 50:
            _pos = '❄️ 築底階段 → 定期定額囤幣、布局下一輪'
        elif eff == 'spring':
            _pos = '🌱 復甦初期 → 分批建倉、佈局主流'
        else:
            _pos = '☀️ 主升/中段 → 持有並設移動止盈'
        st.markdown(f"**有效季節**：{season_label}")
        st.markdown(f"**週期定位**：{_pos}")


def render(btc, chart_df, tvl_hist, stable_hist, fund_hist, curr, dxy, ov, proxies, realtime_data):
    """
    長週期週期羅盤 (Macro Cycle Compass)

    整合 Tab 1 (牛市雷達) + Tab 5 (熊市底部獵人)，
    提供從短週期技術面到長週期鏈上指標的完整宏觀視角。
    """
    st.subheader('🧭 長週期羅盤 (Macro Cycle Compass)')
    st.caption('整合長週期技術指標、鏈上數據與宏觀環境，量化市場所處的週期位置')
    # ov = service/overview.OverviewMetrics（含 fallback 解析後的速覽指標）；此處 unpack
    # 維持下方既有變數名，避免大面積改動
    funding_rate, tvl_val = ov.funding_rate, ov.tvl
    fng_val, fng_state, fng_source = ov.fng_val, ov.fng_state, ov.fng_source
    market_score, _bear_total, _bull_total, _breakdown_rows = calculate_market_cycle_score_breakdown(curr)
    bear_score_now, _ = calculate_bear_bottom_score(curr)
    price = curr['close']
    ma50 = curr.get('SMA_50', price)
    ma200 = curr.get('SMA_200', price)
    ma200_slope = curr.get('SMA_200_Slope', 0) or 0
    mvrv = curr.get('MVRV_Z_Proxy', 0) or 0
    if mvrv > 3.5:
        phase_idx, phase_name, phase_desc = (5, '🔥 狂熱頂部', '風險極高，建議分批止盈。MVRV Z > 3.5 歷史頂部信號。')
    elif price > ma200 and ma50 > ma200 and (ma200_slope > 0):
        phase_idx, phase_name, phase_desc = (4, '🐂 牛市主升段', '多頭排列，年線上揚。策略：持有並設移動止盈。')
    elif price > ma200 and ma50 > ma200 and (ma200_slope <= 0):
        phase_idx, phase_name, phase_desc = (3, '😴 牛市休整/末期', '價格高於年線但動能減弱。策略：輕倉持有，注意反轉。')
    elif price > ma200 and ma50 <= ma200:
        phase_idx, phase_name, phase_desc = (2, '🌱 初牛復甦', '站上年線，等待黃金交叉。策略：分批建倉。')
    elif price <= ma200 and ma50 > ma200:
        phase_idx, phase_name, phase_desc = (1, '📉 轉折回調', '跌破年線，注意死叉風險。策略：輕倉觀望。')
    else:
        phase_idx, phase_name, phase_desc = (0, '❄️ 深熊築底', '均線空頭排列，底部積累區。策略：定投囤幣。')
    level_name, level_color, level_action = _score_meta(market_score)
    st.markdown(f"""
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
        """, unsafe_allow_html=True)
    g_col1, g_col2, g_col3 = st.columns([2, 2, 3])
    with g_col1:
        st.plotly_chart(_build_cycle_gauge(market_score), use_container_width=True)
    with g_col2:
        st.plotly_chart(_build_phase_gauge(phase_idx, phase_name), use_container_width=True)
    with g_col3:
        st.markdown(f'### 📡 {phase_name}')
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
    with st.expander(f'📐 多空評分計算公式（熊底 {_bear_total}/100 分 — 牛頂 {_bull_total}/100 分 = **{market_score:+d}**）', expanded=False):
        st.caption('**公式**：多空評分 = 牛頂分數 − 熊底分數，clip 至 [-100, +100]。8 大鏈上指標各自對熊底與牛頂分別打分，分數根據最新日線即時計算。若分數長時間不變，屬正常現象（代表市場週期位置確實穩定在當前區間，非 bug）。')
        _tbl = []
        for _r in _breakdown_rows:
            _net = _r['bull'] - _r['bear']
            _tbl.append({'指標': _r['name'], '當前值': _r['value'], f'熊底分 (/{_r['bear_max']})': _r['bear'], f'牛頂分 (/{_r['bull_max']})': _r['bull'], '淨貢獻 (牛-熊)': f'{_net:+d}'})
        st.dataframe(pd.DataFrame(_tbl), use_container_width=True, hide_index=True)
        st.caption(f'合計 → 熊底 {_bear_total} 分 ｜ 牛頂 {_bull_total} 分 ｜ 最終分數 = {_bull_total} − {_bear_total} = **{market_score:+d}**')
    st.markdown('---')
    st.subheader('A. 多維度長週期主圖 (BTC Price + On-Chain)')
    cache_key = _make_mc_cache_key(chart_df, tvl_hist, stable_hist, fund_hist)
    ss_hash_key = 'tab_mc_hash'
    ss_main_key = f'tab_mc_fig_main_{cache_key}'
    if st.session_state.get(ss_hash_key) == cache_key and ss_main_key in st.session_state:
        fig_main = st.session_state[ss_main_key]
    else:
        _cdf = chart_df.copy()
        if _cdf.index.tz is not None:
            _cdf.index = _cdf.index.tz_localize(None)
        fig_main = make_subplots(rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.025, row_heights=[0.4, 0.15, 0.15, 0.15, 0.15], subplot_titles=('比特幣價格行為 + MA200 / MA50 (Price Action)', 'AHR999 囤幣指標 (< 0.45 = 歷史抄底區)', '幣安資金費率 (Funding Rate) & RSI_14', 'BTC 鏈上 TVL (DeFiLlama)', '全球穩定幣市值 (Stablecoin Cap)'))
        fig_main.add_trace(go.Candlestick(x=_cdf.index, open=_cdf['open'], high=_cdf['high'], low=_cdf['low'], close=_cdf['close'], name='BTC'), row=1, col=1)
        fig_main.add_trace(go.Scatter(x=_cdf.index, y=_cdf['SMA_200'], line=dict(color='orange', width=2), name='SMA 200'), row=1, col=1)
        fig_main.add_trace(go.Scatter(x=_cdf.index, y=_cdf['SMA_50'], line=dict(color='cyan', width=1.5, dash='dash'), name='SMA 50'), row=1, col=1)
        if 'EMA_20' in _cdf.columns:
            fig_main.add_trace(go.Scatter(x=_cdf.index, y=_cdf['EMA_20'], line=dict(color='#ffeb3b', width=1, dash='dot'), name='EMA 20'), row=1, col=1)
        if 'AHR999' in _cdf.columns and _cdf['AHR999'].notna().any():
            ahr_c = ['#00ff88' if v < 0.45 else '#ffcc00' if v < 0.8 else '#ff8800' if v < 1.2 else '#ff4b4b' for v in _cdf['AHR999'].fillna(1.0)]
            fig_main.add_trace(go.Bar(x=_cdf.index, y=_cdf['AHR999'], marker_color=ahr_c, name='AHR999', showlegend=False), row=2, col=1)
            for lvl, col, lbl in [(0.45, '#00ff88', '抄底 0.45'), (0.8, '#ffcc00', '偏低 0.8'), (1.2, '#ff4b4b', '高估 1.2')]:
                fig_main.add_hline(y=lvl, line_color=col, line_width=1, line_dash='dash', annotation_text=lbl, row=2, col=1)
        if not fund_hist.empty:
            fund_sub = fund_hist.reindex(_cdf.index, method='nearest')
            fund_sub.loc[fund_sub.index < fund_hist.index[0]] = np.nan
            valid_mask = fund_sub['fundingRate'].notna()
            fr_colors = ['#00ff88' if v > 0 else '#ff4b4b' for v in fund_sub.loc[valid_mask, 'fundingRate']]
            fig_main.add_trace(go.Bar(x=fund_sub.index[valid_mask], y=fund_sub.loc[valid_mask, 'fundingRate'], marker_color=fr_colors, name='Funding Rate %'), row=3, col=1)
        if 'RSI_14' in _cdf.columns and _cdf['RSI_14'].notna().any():
            fig_main.add_trace(go.Scatter(x=_cdf.index, y=(_cdf['RSI_14'] - 50) * 0.001, line=dict(color='#a32eff', width=1.5), name='RSI (scaled)'), row=3, col=1)
        # 門檻收回 core 單一來源：原硬編 0.03 與同頁文字燈號（FUNDING_HOT_8H≈0.0274）不一致
        fig_main.add_hline(y=FUNDING_HOT_8H, line_color='#ff4b4b', line_width=0.8, line_dash='dot',
                           annotation_text=f'過熱 {FUNDING_HOT_8H:.4f}%', row=3, col=1)
        fig_main.add_hline(y=FUNDING_BASELINE_8H, line_color='#888888', line_width=0.8, line_dash='dot',
                           annotation_text=f'利率基準 {FUNDING_BASELINE_8H}%', row=3, col=1)
        if not tvl_hist.empty:
            _th = tvl_hist.copy()
            if _th.index.tz is not None:
                _th.index = _th.index.tz_localize(None)
            tvl_sub = _th.reindex(_cdf.index, method='nearest')
            fig_main.add_trace(go.Scatter(x=tvl_sub.index, y=tvl_sub['tvl'] if 'tvl' in tvl_sub.columns else [], mode='lines', fill='tozeroy', line=dict(color='#a32eff'), name='TVL (USD)'), row=4, col=1)
        if not stable_hist.empty:
            stab_sub = stable_hist.reindex(_cdf.index, method='nearest')
            fig_main.add_trace(go.Scatter(x=stab_sub.index, y=stab_sub['mcap'] / 1000000000.0, mode='lines', line=dict(color='#2E86C1'), name='Stablecoin Cap ($B)'), row=5, col=1)
        fig_main.update_layout(height=1000, template='plotly_dark', xaxis_rangeslider_visible=False, legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='right', x=1))
        st.session_state[ss_main_key] = fig_main
        st.session_state[ss_hash_key] = cache_key
    st.plotly_chart(fig_main, use_container_width=True)
    st.markdown('---')
    st.subheader('B. 多空指標評分明細 (Level 1 ~ Level 3)')
    st.markdown('#### Level 1 · 散戶視角 (Price & Sentiment)')
    is_golden = curr['close'] > ma200 and ma50 > ma200
    is_rising = ma200_slope > 0
    struct_state = '多頭共振 (STRONG)' if is_golden and is_rising else '震盪/修正 (WEAK)' if not is_golden else '年線走平 (FLAT)'
    recent_high = btc['high'].iloc[-20:].max()
    prev_high = btc['high'].iloc[-40:-20].max()
    dow_state = '更高的高點 (HH)' if recent_high > prev_high else '高點降低 (LH)'
    l1_cols = st.columns(3)
    _slope_arrow = '↗️ 上升' if is_rising else '↘️ 下降'
    l1_data = [
        ('趨勢結構', struct_state, f'MA200 斜率 {_slope_arrow}', '本地計算 (SMA200 斜率)'),
        ('道氏理論', dow_state, '近 20 日 vs 前 20 日高點', '本地計算 (高低點比較)'),
        ('情緒指數', f'{fng_val:.0f}/100', fng_state, fng_source),
    ]
    for col, (title, val, delta, src) in zip(l1_cols, l1_data):
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{title}</div>
            <div class="metric-value">{val}</div>
            <div class="metric-delta">{delta}</div>
            <div class="metric-source">來源：{src}</div>
        </div>""", unsafe_allow_html=True)
    st.markdown('#### Level 2 · 機構視角 (On-Chain & Derivatives)')
    ahr_val = curr.get('AHR999', float('nan'))
    mvrv_z = curr.get('MVRV_Z_Proxy', 0) or 0
    etf_flow = proxies['etf_flow']
    # 門檻收回 core 單一來源（原硬編 0.03 與 FUNDING_ANN_YELLOW 換算值漂移）；
    # 0.01%/8h 是幣安利率基準＝真中性，貼基準與明顯溢價要分得出來。
    fr_state = ('🔥 多頭過熱' if funding_rate > FUNDING_HOT_8H
                else '🟡 溢價偏多' if funding_rate > FUNDING_BASELINE_8H
                else '🟢 情緒中性' if funding_rate >= 0 else '❄️ 空頭主導')
    ahr_state = '🟢 抄底區間' if ahr_val < 0.45 else '🟡 合理區間' if ahr_val < 1.2 else '🔴 高估區間'
    mvrv_state = '🔥 過熱頂部' if mvrv_z > 3.0 else '🟢 價值低估' if mvrv_z < 0 else '中性區域'
    _tvl_source = getattr(realtime_data, 'tvl_source', None) or 'DeFiLlama'
    _fr_source = getattr(realtime_data, 'funding_rate_source', None) or '模擬值'
    l2_cols = st.columns(5)
    _tvl_display = f'${tvl_val / 1e9:.2f}B' if tvl_val > 1e9 else f'${tvl_val:.2f}B'
    _tvl_delta = '↑ 持續增長' if tvl_val > 0 else '↓ 資金流出'
    _etf_delta = '↑ 機構買盤' if etf_flow > 0 else '↓ 機構拋壓'
    l2_data = [
        ('AHR999 囤幣指標', f'{ahr_val:.3f}', ahr_state, '本地計算 (Price/SMA200 × Price/PowerLaw)'),
        ('MVRV Z-Score', f'{mvrv_z:.2f}', mvrv_state, '本地計算 (Price-SMA200)/σ200'),
        ('BTC 生態 TVL', _tvl_display, _tvl_delta, _tvl_source),
        ('ETF 淨流量(24h)', f'{etf_flow:+.1f}M', _etf_delta, '模擬估算 (價格變化 Proxy)'),
        ('資金費率', f'{funding_rate:.4f}%', fr_state, _fr_source),
    ]
    for col, (title, val, delta, src) in zip(l2_cols, l2_data):
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{title}</div>
            <div class="metric-value">{val}</div>
            <div class="metric-delta">{delta}</div>
            <div class="metric-source">來源：{src}</div>
        </div>""", unsafe_allow_html=True)
    st.markdown('#### Level 3 · 宏觀視角 (Macro)')
    m3_col1, m3_col2, m3_col3, m3_col4 = st.columns(4)
    with m3_col1:
        dxy_is_fb = getattr(dxy, 'is_fallback', False)
        if not dxy.empty and (not dxy_is_fb):
            _btc2 = btc.copy()
            _dxy2 = dxy.copy()
            if _btc2.index.tz is not None:
                _btc2.index = _btc2.index.tz_localize(None)
            if _dxy2.index.tz is not None:
                _dxy2.index = _dxy2.index.tz_localize(None)
            comm = _btc2.index.intersection(_dxy2.index)
            if len(comm) >= 90:
                corr = _btc2.loc[comm]['close'].rolling(90).corr(_dxy2.loc[comm]['close']).iloc[-1]
                corr_val = f'{corr:.2f}' if corr == corr else '—'
                # 2026-07-15 稽核 No.5（T-18 同類名實不符修正）：舊版 corr>=-0.5 一律標
                # 「相關性減弱」，強正相關（如 +0.8）被誤標——正相關不是減弱。改三態：
                #   corr<-0.5 負相關(正常避險)｜-0.5~+0.3 相關性減弱｜>+0.3 正相關(異常)
                if corr != corr:
                    corr_delta = '數據不足'
                elif corr < -0.5:
                    corr_delta = '負相關 (正常避險)'
                elif corr > 0.3:
                    corr_delta = '正相關 (異常/風險偏好切換)'
                else:
                    corr_delta = '相關性減弱'
                st.metric('BTC vs DXY 90d 相關係數', corr_val, corr_delta)
                st.caption('來源：本地計算 (Yahoo Finance DXY)')
            else:
                st.metric('BTC vs DXY 90d', '—', '數據不足')
        else:
            st.metric('BTC vs DXY 90d', '—', 'DXY 數據暫不可用')
    with m3_col2:
        m2_df = fetch_m2_series()
        if not m2_df.empty:
            m2_val = m2_df['m2_billions'].iloc[-1]
            m2_is_fb = getattr(m2_df, 'is_fallback', False)
            m2_src = f'備援值 ({_FALLBACK['m2']['date']})' if m2_is_fb else 'FRED WM2NS'
            st.metric('美國 M2', f'${m2_val:,.0f}B', '貨幣供應量')
            st.caption(f'來源：{m2_src}')
        else:
            st.metric('美國 M2', '—', '數據暫不可用')
    with m3_col3:
        jpy = fetch_usdjpy()
        if jpy.get('rate') is not None:
            jpy_src = jpy.get('source', '備援值')
            st.metric('🇯🇵 USD/JPY', f'¥{jpy['rate']:.2f}', f'{jpy['change_pct']:+.2f}% {jpy['trend']}')
            st.caption(f'來源：{jpy_src}')
        else:
            st.metric('🇯🇵 USD/JPY', '—', '數據暫不可用')
    with m3_col4:
        cpi = fetch_us_cpi_yoy()
        if cpi.get('yoy_pct') is not None:
            cpi_src = cpi.get('source', '備援值')
            st.metric(f'🇺🇸 CPI YoY ({cpi['latest_date']})', f'{cpi['yoy_pct']:.1f}%', cpi['trend'])
            st.caption(f'來源：{cpi_src}')
        else:
            st.metric('🇺🇸 CPI YoY', '—', '數據暫不可用')
    st.markdown('---')
    st.subheader('C. 熊市底部獵人 (Bear Bottom Hunter)')
    st.caption('整合 8 大鏈上+技術指標，量化評估當前是否接近歷史性熊市底部')
    curr_score, curr_signals = calculate_bear_bottom_score(btc.iloc[-1])
    score_level, score_color, score_action = _bear_score_meta(curr_score)
    fig_bb_gauge = go.Figure(go.Indicator(
        mode='gauge+number+delta',
        value=curr_score,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={
            'text': "熊市底部評分<br><span style='font-size:0.8em;color:gray'>Bear Bottom Score</span>",
            'font': {'size': 18},
        },
        delta={
            'reference': 50,
            'increasing': {'color': '#ff4b4b'},
            'decreasing': {'color': '#00ff88'},
        },
        gauge={
            'axis': {'range': [0, 100], 'tickwidth': 1, 'tickcolor': 'white'},
            'bar': {'color': score_color},
            'bgcolor': '#1e1e1e',
            'borderwidth': 2,
            'bordercolor': '#333',
            'steps': [
                {'range': [0, 25],   'color': '#1a3a1a'},
                {'range': [25, 45],  'color': '#2a2a2a'},
                {'range': [45, 60],  'color': '#3a3a1a'},
                {'range': [60, 75],  'color': '#3a2a1a'},
                {'range': [75, 100], 'color': '#3a1a1a'},
            ],
            'threshold': {
                'line': {'color': '#ffffff', 'width': 3},
                'thickness': 0.75,
                'value': curr_score,
            },
        },
    ))
    fig_bb_gauge.update_layout(height=280, template='plotly_dark', paper_bgcolor='#0e1117', font={'color': 'white'})
    bg_c1, bg_c2 = st.columns([1, 1])
    with bg_c1:
        st.plotly_chart(fig_bb_gauge, use_container_width=True)
    with bg_c2:
        st.markdown(f'### {score_level}')
        st.markdown(f'**評分: {curr_score}/100**')
        st.info(f'📋 **操作建議**: {score_action}')
        st.markdown("""
        | 分數區間 | 市場狀態 | 建議行動 |
        |---------|---------|---------|
        | 75-100  | 歷史極值底部 | 全力積累 |
        | 60-75   | 明確底部區間 | 重倉布局 |
        | 45-60   | 可能底部區  | 分批試探 |
        | 25-45   | 震盪修正    | 觀望等待 |
        | 0-25    | 牛市高估    | 持有/減倉 |
        """)
    st.markdown('---')
    st.subheader('C1. 八大指標評分明細')
    st.caption('所有指標均由本地歷史 K 線計算，無需外部 API')
    indicator_cols = st.columns(4)
    for idx, (key, sig) in enumerate(curr_signals.items()):
        col = indicator_cols[idx % 4]
        bar_pct = sig['score'] / sig['max'] * 100
        _key_label = key.replace('_', ' ')
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-title">{_key_label}</div>
            <div class="metric-value">{sig['value']}</div>
            <div class="metric-delta">{sig['label']}</div>
            <div style="background:#333;border-radius:4px;height:6px;margin-top:8px;">
                <div style="background:{score_color};width:{bar_pct:.0f}%;height:6px;border-radius:4px;"></div>
            </div>
            <div style="color:#888;font-size:0.75rem;text-align:right;">{sig['score']}/{sig['max']} 分</div>
            <div class="metric-source">來源：本地計算</div>
        </div>
        """, unsafe_allow_html=True)
    st.markdown('---')
    st.subheader('C2. 歷史熊市底部驗證 (Bear Market Bottoms Map)')
    st.caption('橙色區域 = 已知熊市底部 | 藍線 = 200週均線 | 紅線 = Pi Cycle | 黃線 = 冪律支撐 | 青線 = SMA50')
    bb_cache_key = _make_bb_cache_key(btc)
    ss_hist_key = f'tab_mc_fig_hist_{bb_cache_key}'
    if st.session_state.get('tab_mc_bb_key') == bb_cache_key and ss_hist_key in st.session_state:
        fig_hist = st.session_state[ss_hist_key]
    else:
        fig_hist = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.5, 0.25, 0.25], subplot_titles=('BTC 價格 + 底部指標均線 (對數坐標)', 'Pi Cycle Gap (SMA111 vs 2×SMA350) — 負值觸底信號', 'Puell Multiple Proxy — <0.5 礦工投降底部'))
        fig_hist.add_trace(go.Scatter(x=btc.index, y=btc['close'], mode='lines', name='BTC 價格', line=dict(color='#ffffff', width=1.5)), row=1, col=1)
        if 'SMA_1400' in btc.columns and btc['SMA_1400'].notna().any():
            fig_hist.add_trace(go.Scatter(x=btc.index, y=btc['SMA_1400'], mode='lines', name='200週均線', line=dict(color='#2196F3', width=2)), row=1, col=1)
        if 'SMA_350x2' in btc.columns and btc['SMA_350x2'].notna().any():
            fig_hist.add_trace(go.Scatter(x=btc.index, y=btc['SMA_350x2'], mode='lines', name='2×SMA350 (Pi Cycle上軌)', line=dict(color='#ff4b4b', width=1.5, dash='dash')), row=1, col=1)
        if 'SMA_111' in btc.columns and btc['SMA_111'].notna().any():
            fig_hist.add_trace(go.Scatter(x=btc.index, y=btc['SMA_111'], mode='lines', name='SMA111', line=dict(color='#ff8800', width=1.5)), row=1, col=1)
        if 'PowerLaw_Support' in btc.columns and btc['PowerLaw_Support'].notna().any():
            fig_hist.add_trace(go.Scatter(x=btc.index, y=btc['PowerLaw_Support'], mode='lines', name='冪律支撐線', line=dict(color='#ffcc00', width=1.5, dash='dot')), row=1, col=1)
        for b_start, b_end, b_label in KNOWN_BOTTOMS:
            try:
                fig_hist.add_vrect(x0=b_start, x1=b_end, fillcolor='rgba(255,140,0,0.15)', layer='below', line_width=0, annotation_text=b_label, annotation_position='top left', row=1, col=1)
            except Exception:
                pass
        if 'PiCycle_Gap' in btc.columns and btc['PiCycle_Gap'].notna().any():
            pi_c = ['#ff4b4b' if v > 0 else '#00ff88' for v in btc['PiCycle_Gap'].fillna(0)]
            fig_hist.add_trace(go.Bar(x=btc.index, y=btc['PiCycle_Gap'], marker_color=pi_c, name='Pi Cycle Gap (%)', showlegend=False), row=2, col=1)
            fig_hist.add_hline(y=0, line_color='white', line_width=1, opacity=0.5, row=2, col=1)
            fig_hist.add_hline(y=-5, line_color='#00ff88', line_width=1, line_dash='dash', annotation_text='底部信號線', row=2, col=1)
        if 'Puell_Proxy' in btc.columns and btc['Puell_Proxy'].notna().any():
            fig_hist.add_trace(go.Scatter(x=btc.index, y=btc['Puell_Proxy'], mode='lines', line=dict(color='#a32eff', width=1.5), name='Puell Proxy', showlegend=False), row=3, col=1)
            fig_hist.add_hline(y=0.5, line_color='#00ff88', line_width=1.5, line_dash='dash', annotation_text='0.5 底部線', row=3, col=1)
            fig_hist.add_hline(y=4.0, line_color='#ff4b4b', line_width=1.5, line_dash='dash', annotation_text='4.0 頂部線', row=3, col=1)
        fig_hist.update_layout(height=850, template='plotly_dark', xaxis_rangeslider_visible=False, legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1))
        fig_hist.update_yaxes(type='log', row=1, col=1)
        st.session_state[ss_hist_key] = fig_hist
        st.session_state['tab_mc_bb_key'] = bb_cache_key
    st.plotly_chart(fig_hist, use_container_width=True)
    st.markdown('---')
    st.subheader('C3. 歷史底部評分走勢 (Bottom Score History)')
    ss_score_key = f'tab_mc_fig_score_{bb_cache_key}'
    if st.session_state.get('tab_mc_bb_key') == bb_cache_key and ss_score_key in st.session_state:
        fig_score = st.session_state[ss_score_key]
    else:
        score_slice = btc.tail(365 * 4).copy()
        with st.spinner('正在計算歷史底部評分...'):
            score_slice['BottomScore'] = score_series(score_slice)
        fig_score = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=[0.4, 0.6], subplot_titles=('底部評分 (0-100)', 'BTC 價格 (對數)'))
        sc_colors = ['#ff4b4b' if s < 25 else '#ffcc00' if s < 45 else '#ff8800' if s < 60 else '#00ccff' for s in score_slice['BottomScore']]
        fig_score.add_trace(go.Bar(x=score_slice.index, y=score_slice['BottomScore'], marker_color=sc_colors, name='底部評分', showlegend=False), row=1, col=1)
        fig_score.add_hline(y=60, line_color='#00ccff', line_dash='dash', annotation_text='60分 積極積累線', row=1, col=1)
        fig_score.add_hline(y=45, line_color='#ffcc00', line_dash='dot', annotation_text='45分 試探線', row=1, col=1)
        fig_score.add_trace(go.Scatter(x=score_slice.index, y=score_slice['close'], mode='lines', name='BTC 價格', line=dict(color='#ffffff', width=1.5)), row=2, col=1)
        high_score = score_slice[score_slice['BottomScore'] >= 60]
        if not high_score.empty:
            fig_score.add_trace(go.Scatter(x=high_score.index, y=high_score['close'], mode='markers', name='底部積累區 (≥60分)', marker=dict(color='#00ccff', size=5, symbol='circle', opacity=0.7)), row=2, col=1)
        fig_score.update_yaxes(type='log', row=2, col=1)
        fig_score.update_layout(height=600, template='plotly_dark', legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1))
        st.session_state[ss_score_key] = fig_score
    st.plotly_chart(fig_score, use_container_width=True)
    st.markdown('---')
    st.subheader('C4. 當前關鍵底部指標一覽')
    curr_row = btc.iloc[-1]
    _nan = float('nan')
    summary_data = {
        '指標': [
            'AHR999 囤幣指標', 'MVRV Z-Score (Proxy)', 'Pi Cycle Gap',
            '200週均線比值', 'Puell Multiple (Proxy)', '月線 RSI',
            '冪律支撐倍數', 'Mayer Multiple',
        ],
        '當前值': [
            f"{curr_row.get('AHR999', _nan):.3f}",
            f"{curr_row.get('MVRV_Z_Proxy', _nan):.2f}",
            f"{curr_row.get('PiCycle_Gap', _nan):.1f}%",
            f"{curr_row.get('SMA200W_Ratio', _nan):.2f}x",
            f"{curr_row.get('Puell_Proxy', _nan):.2f}",
            f"{curr_row.get('RSI_Monthly', _nan):.1f}",
            f"{curr_row.get('PowerLaw_Ratio', _nan):.1f}x",
            f"{curr_row.get('Mayer_Multiple', _nan):.2f}x",
        ],
        '底部閾值': ['< 0.45', '< 0', '< -5%', '< 1.0x', '< 0.5', '< 30', '< 2x', '< 0.8x'],
        '頂部閾值': ['> 1.2', '> 3.5', '> 10%', '> 4x', '> 4.0', '> 75', '> 10x', '> 2.4x'],
    }
    st.dataframe(pd.DataFrame(summary_data), use_container_width=True, hide_index=True)

    # ── 📍 波段雷達（逃頂＋抄底雙向相對評分，取代舊 C5/C6）──
    _render_swing_radar(btc, curr, funding_rate, fng_val, realtime_data)

    st.markdown('---')
    st.subheader('D. 🗓️ 四季理論目標價預測 (Halving Cycle Forecast)')
    st.caption('依比特幣減半週期（約4年）劃分四季，整合歷史漲跌倍數與冪律模型，預測未來12個月牛市最高價或熊市最低價。')
    current_price = float(btc.iloc[-1]['close'])
    fc = forecast_price(current_price, df=btc)
    if fc is None:
        st.error('無法取得減半週期資訊，請確認數據範圍。')
    else:
        si = fc['season_info']
        eff = fc['effective_season']
        ms = fc['market_state']
        is_corrected = fc.get('is_season_corrected', False)
        eff_color = _season_css_color(eff['season'])
        time_color = _season_css_color(si['season'])
        drawdown_pct = abs(ms['drawdown_from_ath']) * 100
        sma200_val = ms['sma200']
        above_str = '✅ 站上' if ms['is_above_sma200'] else '❌ 跌破'
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
        _cycle_idx_1based = fc['current_cycle_idx'] + 1
        _halving_str = si['halving_date'].strftime('%Y-%m-%d')
        _drawdown_color = '#ff6b6b' if drawdown_pct > 15 else '#ffd93d'
        st.markdown(f"""
            <div style="
                background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                border: 2px solid {eff_color};
                border-radius: 12px;
                padding: 20px 28px;
                margin-bottom: 16px;
            ">
                {season_header}
                <div style="color:#ccc; margin-top:10px; font-size:0.95rem;">
                    第 <b style="color:white">{_cycle_idx_1based}</b> 次減半週期
                    &nbsp;｜&nbsp;
                    減半日: <b style="color:white">{_halving_str}</b>
                    &nbsp;｜&nbsp;
                    已過 <b style="color:white">{si['days_since']}</b> 天 /
                    距下次減半還有 <b style="color:white">{si['days_to_next']}</b> 天
                </div>
                <div style="color:#aaa; margin-top:6px; font-size:0.88rem; display:flex; gap:24px; flex-wrap:wrap;">
                    <span>週期月份: <b style="color:white">第 {si['month_in_cycle']} 個月</b></span>
                    <span>週期進度: <b style="color:white">{si['cycle_progress'] * 100:.1f}%</b></span>
                    <span>距ATH跌幅: <b style="color:{_drawdown_color}">
                        -{drawdown_pct:.1f}%</b> (ATH ${ms['cycle_ath']:,.0f})</span>
                    <span>200日均線: <b style="color:white">{above_str} ${sma200_val:,.0f}</b></span>
                </div>
            </div>
            """, unsafe_allow_html=True)
        if is_corrected and fc.get('correction_reason'):
            st.warning(fc['correction_reason'])
        st.plotly_chart(_render_season_timeline(si, effective_season=eff['season']), use_container_width=True)
        st.markdown('---')
        # ── 底部估計（🗓️ 四季雷達定位 + D2 明細共用同一 be，避免重算與漂移）──
        be = None
        try:
            _hr = fetch_hashrate_history_ths()
            _lh = _hr[max(_hr)] if _hr else None
            be = compute_all_bottom_estimates(current_price, df=btc, hashrate_ths=_lh,
                                              onchain=get_latest_bottom_metrics())
        except Exception as _be_err:
            _warn_unavailable('底部資料', _be_err)

        # ── 🗓️ 四季雷達 · 本輪頂底定位（週期頂錨 + 四季論底 + 牛頂/熊底分，整合原 C5-B）──
        _render_season_radar(btc, fc, be, current_price)
        st.markdown('---')

        # ── D2 底部支撐綜合評估明細（core/bottom_floors，與四季雷達共用 be，與每日 LINE 同源）──
        st.markdown('#### D2. 🛡️ 底部支撐綜合評估')
        st.caption('整合四季論趨勢底 + 200週均線/冪律/礦工成本 + 鏈上錨（Realized/Balanced/CVDD）+ 技術錨（Mayer/AHR999），與每日 LINE 推播同源。')
        if be and be.get('estimates'):
            _kc = {'season': '#ef5350', 'floor': '#42a5f5', 'anchor': '#ffd54f', 'warning': '#ff9800'}
            mcol1, mcol2 = st.columns(2)
            with mcol1:
                fl = be.get('final_low')
                # C-R3：外插 n=3 點估精度撐不起防守決策 → 併列悲觀/樂觀區間
                _fld, _fls = be.get('final_low_deep'), be.get('final_low_shallow')
                _range_html = (f'<div style="color:#ff8a65;font-size:0.78rem;">區間 ${_fld:,.0f} ~ ${_fls:,.0f}（悲觀~樂觀）</div>'
                               if (_fld and _fls) else '')
                st.markdown(f"""<div style="background:#1a1a2e;border:2px solid #ef5350;border-radius:10px;padding:14px;text-align:center;">
                    <div style="color:#888;font-size:0.8rem;">最終最低價估計</div>
                    <div style="color:#ef5350;font-size:1.8rem;font-weight:800;">${fl:,.0f}</div>
                    {_range_html}
                    <div style="color:#666;font-size:0.72rem;">依據 {be.get('final_low_basis') or '—'}</div>
                </div>""" if fl else '—', unsafe_allow_html=True)
            with mcol2:
                en = be.get('ensemble_low')
                st.markdown(f"""<div style="background:#1a1a2e;border:1px solid #66bb6a;border-radius:10px;padding:14px;text-align:center;">
                    <div style="color:#888;font-size:0.8rem;">可靠度加權中位數</div>
                    <div style="color:#66bb6a;font-size:1.8rem;font-weight:800;">${en:,.0f}</div>
                    <div style="color:#666;font-size:0.72rem;">可靠度加權中位數</div>
                </div>""" if en else '—', unsafe_allow_html=True)
            # 各算法明細預設收起（總結兩卡已給結論，明細供查證用）
            with st.expander(f"📋 各算法明細（{len(be['estimates'])} 個底部錨）", expanded=False):
                _rows = ''
                for e in sorted(be['estimates'], key=lambda x: -x['value']):
                    buf = (current_price - e['value']) / e['value'] * 100 if e['value'] else 0
                    bclr = '#66bb6a' if buf >= 0 else '#ef5350'
                    rel = e.get('reliability', 50)
                    rclr = '#66bb6a' if rel >= 75 else '#ffd54f' if rel >= 62 else '#ff8a65'
                    _rows += (f"<tr><td style='padding:4px 8px;color:{_kc.get(e['kind'],'#ccc')};'>{e['label']}</td>"
                              f"<td style='padding:4px 8px;text-align:right;color:#fff;font-weight:600;'>${e['value']:,.0f}</td>"
                              f"<td style='padding:4px 8px;text-align:right;color:{bclr};'>{'+' if buf>=0 else ''}{buf:.0f}%</td>"
                              f"<td style='padding:4px 8px;text-align:right;color:{rclr};'>{rel}</td>"
                              f"<td style='padding:4px 8px;color:#777;font-size:0.72rem;'>{e['note']}</td></tr>")
                st.markdown(f"""<table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-top:8px;">
                    <tr style="color:#aaa;border-bottom:1px solid #444;">
                      <th style="text-align:left;padding:4px 8px;">算法</th><th style="text-align:right;padding:4px 8px;">最低價</th>
                      <th style="text-align:right;padding:4px 8px;">現價距此</th><th style="text-align:right;padding:4px 8px;">可靠度</th>
                      <th style="text-align:left;padding:4px 8px;">說明</th></tr>
                    {_rows}</table>
                    <div style="color:#777;font-size:0.72rem;margin-top:6px;">
                    紅=四季論趨勢底　藍=硬地板　黃=鏈上/技術錨　橙=警示(牛末常被跌破至~0.67×)。
                    final_low = max(四季論趨勢底, 礦工電費硬地板)——歷史三輪熊底從未跌破純電費。</div>""",
                    unsafe_allow_html=True)
                if be.get('asof'):
                    st.caption(f"鏈上資料 as of {be['asof']}（bitcoin-data.com）")
        else:
            st.caption('底部綜合評估暫不可用')

        st.markdown('#### D3. 目標價走勢圖（過去2年 + 未來12個月）')
        ss_fc_key = f'tab_mc_fig_fc_{bb_cache_key}'
        if st.session_state.get('tab_mc_bb_key') == bb_cache_key and ss_fc_key in st.session_state:
            fig_fc = st.session_state[ss_fc_key]
        else:
            with st.spinner('建立預測走勢圖...'):
                fig_fc = _render_forecast_chart(btc, fc)
            st.session_state[ss_fc_key] = fig_fc
        st.plotly_chart(fig_fc, use_container_width=True)
        st.markdown('---')
        st.markdown('#### D4. 歷史減半週期比較')
        st.caption('✅ = 完整週期 ｜ 🔄 = 進行中')
        col_tbl, col_bar = st.columns([1.3, 1])
        with col_tbl:
            st.dataframe(get_cycle_comparison_table(df=btc), use_container_width=True, hide_index=True)
        with col_bar:
            st.plotly_chart(_render_cycle_waterfall(fc), use_container_width=True)
        st.markdown('---')
        st.markdown('#### D5. 四季操作策略')
        strat_cols = st.columns(4)
        strategies = [
            ('🌱', '春季 (月0-11)',  '#1b5e20', '減半後復甦期。市場情緒由恐懼轉向觀望，適合**分批建倉**，重點佈局主流幣。'),
            ('☀️', '夏季 (月12-23)', '#f57f17', '牛市加速期。FOMO情緒蔓延，適合**持有並設置移動止盈**，避免頂部加倉。'),
            ('🍂', '秋季 (月24-35)', '#e65100', '泡沫破裂期。高點已過，空頭確立，適合**逐步減倉**，轉向穩定資產。'),
            ('❄️', '冬季 (月36-47)', '#0d47a1', '熊市底部期。恐慌拋售為主，適合**定期定額囤幣**，等待下一個春天。'),
        ]
        for col, (emoji, name, bg, desc) in zip(strat_cols, strategies):
            is_current = name.startswith(eff['emoji']) or name.startswith(si['emoji'])
            border = f'2px solid {eff_color}' if is_current else '1px solid #333'
            cur_tag = f"<div style='color:{eff_color};font-size:0.8rem;margin-top:8px;font-weight:600;'>← 當前季節</div>" if is_current else ''
            col.markdown(f"""
                <div style="background:{bg}22;border:{border};border-radius:10px;padding:14px;min-height:160px;">
                    <div style="font-size:1.6rem;">{emoji}</div>
                    <div style="color:white;font-weight:600;margin:4px 0;">{name}</div>
                    <div style="color:#ccc;font-size:0.82rem;">{desc}</div>
                    {cur_tag}
                </div>""", unsafe_allow_html=True)
    st.markdown("""
    ---
    > **免責聲明**: 以上指標均為技術分析工具，不構成投資建議。
    > 歷史數據不代表未來表現。加密貨幣市場波動劇烈，請嚴格控制倉位風險。
    > Pi Cycle 冪律模型參數來源: Giovanni Santostasi 比特幣冪律理論。
    > 四季理論基於歷史減半週期規律，每個週期漲幅遞減為已知趨勢，實際結果可能顯著偏離。
    """)