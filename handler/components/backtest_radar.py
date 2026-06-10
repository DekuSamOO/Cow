"""
Backtest Sub-Tab 6：波段雷達歷史回放（逃頂 / 抄底 / 趨勢）

逐日重放 core/relative_high、relative_low、trend_direction 的歷史分數序列，
疊在價格上視覺化，並產出「分數跨越門檻 → 其後 60 日報酬分布」統計，
作為未來重校 ESCAPE_ALERT_THRESHOLD=60 的依據。

回放口徑（重要）：
  只用歷史可得輸入 — 技術/長週期/趨勢（日線派生）+ 資金費率（2021+）+ F&G（2018+）。
  OI / ETF / SOPR / BTC.D / 總經與線上灰燈一致給 0 分 → 回放分數為保守下界。
  可得天花板：逃頂 55（資費20+技術25+F&G10）、抄底 65（週期25+技術20+負費率10+F&G10）。
"""
import hashlib

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from core.radar_replay import (DIV_WINDOW, escape_score_series, low_score_series,
                               threshold_forward_stats, trend_score_series)
from handler._cache import CowCacheNamespace

_ns = CowCacheNamespace("backtest_radar")

# 背離視窗（單一來源 core/radar_replay.DIV_WINDOW）：切片時多留這段暖機
_WARMUP = DIV_WINDOW


@st.cache_data(ttl=86400, show_spinner=False)
def _fng_map() -> dict:
    from service.realtime import fetch_fng_history
    return fetch_fng_history()


@st.cache_data(ttl=86400, show_spinner=False)
def _fund_daily() -> pd.Series:
    """資金費率日均 8h%（2021+）。失敗回空 Series（該子項回放為 0 分）。"""
    try:
        from service.onchain import fetch_aux_history
        _, _, fund = fetch_aux_history()
        if fund is not None and not fund.empty and "fundingRate" in fund.columns:
            f = fund.copy()
            if f.index.tz is not None:
                f.index = f.index.tz_localize(None)
            return f["fundingRate"].resample("D").mean()
    except Exception:
        pass
    return pd.Series(dtype=float)


def _replay(btc: pd.DataFrame, radar: str, years: int):
    """回放指定雷達，session_state 快取（同設定同資料只算一次）。"""
    suffix = hashlib.md5(
        f"{btc.index[-1]}|{len(btc)}|{radar}|{years}".encode()).hexdigest()[:16]
    if _ns.contains(suffix):
        return _ns.get(suffix)

    sub = btc.tail(years * 365 + _WARMUP)
    if radar == "trend":
        scores = trend_score_series(sub, start=_WARMUP)
    else:
        fund, fng = _fund_daily(), _fng_map()
        fn = escape_score_series if radar == "escape" else low_score_series
        scores = fn(sub, fund_daily=fund, fng_map=fng, start=_WARMUP)
    _ns.set(suffix, scores)
    return scores


def _fig(close: pd.Series, scores: pd.Series, radar: str) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.55, 0.45],
                        vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=close.index, y=close, name="BTC", line=dict(color="#888888", width=1.2)),
                  row=1, col=1)
    color = {"escape": "#E74C3C", "low": "#27AE60", "trend": "#2980B9"}[radar]
    fig.add_trace(go.Scatter(x=scores.index, y=scores, name="分數", line=dict(color=color, width=1.4)),
                  row=2, col=1)
    if radar == "trend":
        for y, dash in ((0, "solid"), (50, "dot"), (-50, "dot")):
            fig.add_hline(y=y, line_dash=dash, line_color="#AAAAAA", row=2, col=1)
        fig.update_yaxes(range=[-100, 100], title_text="淨方向分", row=2, col=1)
    else:
        for thr, dash in ((60, "dash"), (75, "dot"), (45, "dot")):
            fig.add_hline(y=thr, line_dash=dash, line_color="#AAAAAA", row=2, col=1)
        fig.update_yaxes(range=[0, 100], title_text="分數", row=2, col=1)
    fig.update_yaxes(type="log", title_text="USD (log)", row=1, col=1)
    fig.update_layout(height=520, margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.06), hovermode="x unified")
    return fig


def render(btc):
    st.markdown("#### 📡 波段雷達歷史回放")
    st.caption(
        "逐日重放逃頂/抄底/趨勢分數（與 dashboard / LINE / BTC_WATCH 同源 core 邏輯）。"
        "僅用歷史可得輸入：技術＋長週期（全期）、資金費率（2021+）、F&G（2018+）；"
        "OI/ETF/SOPR/BTC.D/總經與線上灰燈一致為 0 → **分數為保守下界**"
        "（可得天花板：逃頂 55、抄底 65）。"
    )

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        choice = st.selectbox("雷達", ["🚨 逃頂", "🟢 抄底", "🧭 趨勢方向"], key="radar_replay_sel")
    with c2:
        years = st.slider("回放年數", 2, 10, 4, key="radar_replay_years")
    with c3:
        st.write("")
        run = st.button("開始回放", key="radar_replay_btn", use_container_width=True)

    radar = {"🚨 逃頂": "escape", "🟢 抄底": "low", "🧭 趨勢方向": "trend"}[choice]
    state_key = f"done_{radar}_{years}"
    if not run and not _ns.contains(state_key):
        st.info("選好雷達與年數後按「開始回放」（首次計算約 10–30 秒，之後同設定走快取）。")
        return

    with st.spinner("逐日回放中…"):
        scores = _replay(btc, radar, years)
    _ns.set(state_key, True)
    if scores.empty:
        st.warning("回放序列為空（資料不足）。")
        return

    close = btc["close"].reindex(scores.index)
    st.plotly_chart(_fig(close, scores, radar), use_container_width=True)

    if radar == "trend":
        share = {
            "強多頭 (≥+50)": (scores >= 50).mean(),
            "多頭 (+20~+50)": ((scores >= 20) & (scores < 50)).mean(),
            "盤整 (±20)": (scores.abs() < 20).mean(),
            "空頭 (-50~-20)": ((scores <= -20) & (scores > -50)).mean(),
            "強空頭 (≤-50)": (scores <= -50).mean(),
        }
        st.dataframe(pd.DataFrame({"佔比": share}).style.format("{:.1%}"),
                     use_container_width=True)
        return

    mode = "top" if radar == "escape" else "bottom"
    stats = threshold_forward_stats(scores, btc["close"], thresholds=(45, 60, 75),
                                    horizon=60, mode=mode)
    hit_help = "其後60日最大回撤 ≤ -18%" if mode == "top" else "其後60日最大漲幅 ≥ +18%"
    st.markdown(f"**門檻跨越事件 → 其後 60 日報酬分布**（命中 = {hit_help}，與權重擬合定義一致）")
    st.dataframe(
        stats.style.format({"命中率": "{:.0%}", "中位最大跌幅": "{:+.1%}",
                            "中位最大漲幅": "{:+.1%}", "中位期末報酬": "{:+.1%}"},
                           na_rep="—"),
        use_container_width=True, hide_index=True,
    )
    st.caption("⚠️ 回放分數缺 OI/ETF 等強訊號維度，事件數與命中率僅供門檻重校參考，"
               "非線上警報的歷史重現。")
