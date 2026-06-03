"""
app.py — 比特幣投資戰情室 (Bitcoin Command Center)  ·  v2.0
薄層入口點：負責組合各層模組，不含業務邏輯

架構分層:
  core/       — 純計算 (指標、評分)，無 Streamlit 依賴
  service/    — 數據獲取 (市場數據、鏈上、即時)
  strategy/   — 策略引擎 (波段、雙幣)
  handler/    — Streamlit UI (每個 Tab 為獨立函數)

v2.0 重構:
  - 新增「今日大盤速覽 (Overview)」橫向 Metric 區塊
  - 側邊欄精簡化：只保留日期區間
  - Tab 1 (牛市雷達) + Tab 5 (熊市底部獵人) 合併為「長週期週期羅盤」
  - 各 Tab 專屬參數移至對應 Tab 內部設定
"""
import math
import time
import pandas as pd
import streamlit as st
from datetime import datetime

# Handler 層
from handler.layout import setup_page, render_sidebar
import handler.tab_macro_compass as tab1_handler   # 長週期週期羅盤 (原 Tab1+Tab5)
import handler.tab_swing          as tab2_handler
import handler.tab_dual_invest    as tab3_handler
import handler.tab_backtest       as tab4_handler

# Service 層
from service.market_data import fetch_market_data
from service.onchain import fetch_aux_history
from service.realtime import fetch_realtime_data, RealtimeData
from service.overview import resolve_overview_metrics
from service.news import (
    fetch_crypto_news, humanize_age, summarize_sentiment, SENTIMENT_EMOJI,
)
from service.mock import get_realtime_proxies

# Core 層
from core.indicators import calculate_technical_indicators, calculate_ahr999
from core.bear_bottom import calculate_bear_bottom_indicators


# ==============================================================================
# 0. 即時大盤速覽 Fragment（每 60 秒自動重跑，不觸發全頁重載）
# ==============================================================================
@st.fragment(run_every=60)
def render_realtime_overview(
    prev_close: float,
    fallback_price: float,
    rsi14: float,
    sma50: float,
    ahr999: float,
):
    """即時大盤速覽：BTC 價格、恐懼貪婪、資金費率、TVL、AHR999、穩定幣市值
    只接收純量參數，避免大型 DataFrame 序列化導致 fragment 重跑失敗。
    """
    # 若主流程已在 30 秒內抓取過，直接重用快取，避免重複打 API
    _rt_cache = st.session_state.get('_rt_cache', {})
    if _rt_cache and (time.time() - _rt_cache.get('ts', 0)) < 30:
        rt = _rt_cache['data']
    else:
        try:
            rt = fetch_realtime_data()
        except Exception:
            rt = RealtimeData(is_mocked=True)

    m = resolve_overview_metrics(
        rt, fallback_price=fallback_price, rsi14=rsi14, sma50=sma50,
    )
    current_price = m.price
    _price_source = m.price_source
    _funding_rate = m.funding_rate
    _tvl_val      = m.tvl
    _fng_val      = m.fng_val
    _fng_state    = m.fng_state
    _fng_source   = m.fng_source

    st.caption(
        f"數據更新時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 核心版本: Antigravity v4"
    )
    st.markdown("### 📊 今日大盤速覽")
    _c1, _c2, _c3, _c4, _c5, _c6 = st.columns(6)

    _price_chg = (current_price - prev_close) / prev_close * 100
    _c1.metric(
        "💰 BTC 當前價格",
        f"${current_price:,.0f}",
        f"{_price_chg:+.2f}%",
        delta_color="normal",
    )
    _c1.caption(f"來源：{_price_source}")

    _c2.metric(
        "😱 恐懼貪婪指數",
        f"{_fng_val:.0f}/100",
        _fng_state,
        delta_color="normal" if _fng_val >= 50 else "inverse",
    )
    _c2.caption(f"來源：{_fng_source}")

    if m.funding_is_real:
        _fr_delta = "🔥 多頭過熱" if _funding_rate > 0.03 else ("🟢 中性" if _funding_rate > 0 else "❄️ 空頭")
        _c3.metric(
            "💸 資金費率",
            f"{_funding_rate:.4f}%",
            _fr_delta,
            delta_color="inverse" if _funding_rate > 0.03 else "normal",
        )
        _c3.caption(f"來源：{m.funding_source}")
    else:
        _c3.metric("💸 資金費率", "—", "⚠️ 資料暫缺")
        _c3.caption("來源：即時 API 連線失敗")

    if m.tvl_is_real:
        _tvl_display = f"${_tvl_val/1e9:.2f}B" if _tvl_val > 1e9 else f"${_tvl_val:.2f}M"
        _c4.metric("🏦 BTC 生態 TVL", _tvl_display, "↑ 鏈上活躍" if _tvl_val > 0 else "—")
        _c4.caption(f"來源：{m.tvl_source}")
    else:
        _c4.metric("🏦 BTC 生態 TVL", "—", "⚠️ 資料暫缺")
        _c4.caption("來源：即時 API 連線失敗")

    if not math.isnan(ahr999):
        _ahr_state = "🟢 抄底區" if ahr999 < 0.45 else ("🟡 合理區" if ahr999 < 1.2 else "🔴 高估區")
        _c5.metric("📐 AHR999", f"{ahr999:.3f}", _ahr_state)
    else:
        _c5.metric("📐 AHR999", "—", "計算中")
    _c5.caption("來源：冪律模型 (Santostasi)")

    _stab_mcap = rt.stablecoin_mcap
    if _stab_mcap and _stab_mcap > 0:
        _c6.metric(
            "💵 穩定幣市值",
            f"${_stab_mcap:.1f}B",
            "↑ 流動性充沛" if _stab_mcap > 100 else "流動性一般",
        )
        _c6.caption("來源：DeFiLlama")
    else:
        _c6.metric("💵 穩定幣市值", "—", "連線中")
        _c6.caption("來源：連線失敗")

    st.markdown("---")


# ==============================================================================
# 0b. 加密貨幣熱門新聞（4h 快取＋Gemini 中文化，不接 fragment — 避免 list 序列化靜默失效）
# ==============================================================================
# 分類 filter 關鍵字（比對標題/中文標題/小結/標籤）
_NEWS_CATEGORIES = {
    "全部": None,
    "BTC":  ("bitcoin", "btc", "比特幣", "satoshi"),
    "ETH":  ("ethereum", "eth", "以太", "vitalik"),
    "DeFi": ("defi", "stablecoin", "穩定幣", "去中心", "yield", "tvl"),
    "法規": ("regulat", "sec", "lawsuit", "court", "ban", "法規", "監管",
             "congress", "senate", "government", "tax", " law"),
}
def _match_news_category(item, keys) -> bool:
    if keys is None:
        return True
    hay = " ".join([
        item.title or "", item.title_zh or "",
        item.summary_zh or "", " ".join(item.tags or []),
    ]).lower()
    return any(k in hay for k in keys)


def render_news_panel(limit: int = 8):
    """社群熱門新聞區塊：多來源聚合、Gemini 中文化標題＋小結、情緒燈號、分類 filter。
    走 @st.cache_data(ttl=14400) 隨整頁重整刷新（搭配持久化翻譯快取省 Gemini token）。
    刻意不掛 @st.fragment（CLAUDE.md 陷阱 #1：list 傳入 fragment 會序列化失敗）。
    """
    feed = fetch_crypto_news(limit=limit)

    _hdr_l, _hdr_r = st.columns([3, 1])
    _hdr_l.markdown("### 📰 加密貨幣熱門新聞")
    _src_note = "⚠️ 即時來源連線失敗，顯示備援連結" if feed.is_fallback else f"來源：{feed.source}"
    _hdr_r.caption(_src_note)

    if not feed.items:
        st.info("目前無法取得新聞，請稍後重新整理。")
        st.markdown("---")
        return

    # 情緒燈號彙總（A-1）：依各則 AI 情緒判定整體輿情（與每日推播共用 summarize_sentiment）
    _sent = summarize_sentiment(feed.items)
    if _sent.has_data:
        _m_l, _m_r = st.columns([1, 2])
        _m_l.markdown(f"**今日輿情：{_sent.mood}**")
        _m_r.caption(
            f"🟢 偏多 {_sent.bull}　🔴 偏空 {_sent.bear}　⚪ 中性 {_sent.neutral}"
            "（AI 情緒判定，與恐懼貪婪指數互補）"
        )

    # 社群 24h 熱搜（CoinGecko Trending，取代被 IP 封鎖的 Reddit）
    if feed.trending:
        st.caption("🔥 社群 24h 熱搜：" + "　".join(f"`{s}`" for s in feed.trending))

    # 分類 filter（A-3）
    _cat = st.radio(
        "新聞分類", list(_NEWS_CATEGORIES.keys()),
        horizontal=True, label_visibility="collapsed", key="news_cat",
    )
    _filtered = [it for it in feed.items if _match_news_category(it, _NEWS_CATEGORIES[_cat])]
    if not _filtered:
        st.caption(f"「{_cat}」分類目前沒有相關新聞。")
        st.markdown("---")
        return

    # 兩欄卡片：中文標題（無翻譯則 fallback 英文）＋中文小結＋情緒 emoji＋來源時間
    _cols = st.columns(2)
    for _i, _it in enumerate(_filtered):
        with _cols[_i % 2]:
            _emoji = SENTIMENT_EMOJI.get(_it.sentiment or "", "")
            _title = _it.title_zh or _it.title
            st.markdown(f"{_emoji} **[{_title}]({_it.url})**")
            if _it.summary_zh:
                st.caption(_it.summary_zh)
            _meta = " · ".join(p for p in (_it.source, humanize_age(_it.published_at)) if p)
            st.caption(f"🔗 {_meta}")

    st.markdown("---")


# ==============================================================================
# 1. 頁面初始化
# ==============================================================================
setup_page()
sidebar_params = render_sidebar()

# v2.0: 只從 sidebar 取日期區間（其餘參數已移至各 Tab）
c_start = sidebar_params["c_start"]
c_end   = sidebar_params["c_end"]

# ==============================================================================
# 2. 數據載入（含錯誤邊界與降級方案）
# ==============================================================================
_data_warnings = []

with st.spinner("正在連線至戰情室數據庫..."):
    # --- BTC 歷史數據（唯一致命依賴）---
    try:
        btc, dxy = fetch_market_data()
    except Exception as e:
        btc, dxy = pd.DataFrame(), pd.DataFrame()
        _data_warnings.append(f"市場數據載入異常: {e}")

    if btc.empty:
        st.error("❌ 無法取得 BTC 歷史數據（四層備援 Yahoo / Binance / Kraken / CryptoCompare 均失敗）。")
        st.info("💡 可能原因：網路不通、所有 API 暫時限速。請等待 5 分鐘後重新整理頁面（快取 TTL 為 300 秒）。")
        st.stop()

    # 指標計算
    try:
        btc = calculate_technical_indicators(btc)
        btc = calculate_ahr999(btc)
        btc = calculate_bear_bottom_indicators(btc)
    except Exception as e:
        _data_warnings.append(f"指標計算部分失敗: {e}")

    # 鏈上輔助數據（非致命）
    try:
        tvl_hist, stable_hist, fund_hist = fetch_aux_history()
    except Exception as e:
        import pandas as _pd
        tvl_hist = stable_hist = fund_hist = _pd.DataFrame()
        _data_warnings.append(f"鏈上數據載入失敗 (TVL/穩定幣/資金費率)，顯示空白: {e}")

    # 即時數據（非致命）
    try:
        realtime_data = fetch_realtime_data()
        # 存入 session_state 快取，供 fragment 首次觸發時重用（TTL 30s）
        st.session_state['_rt_cache'] = {'data': realtime_data, 'ts': time.time()}
    except Exception as e:
        realtime_data = RealtimeData(is_mocked=True)
        _data_warnings.append(f"即時數據載入失敗，使用模擬數據: {e}")

    curr = btc.iloc[-1]

    # 速覽指標降級解析（與 fragment 共用單一 helper，避免重複邏輯）
    _ov = resolve_overview_metrics(
        realtime_data,
        fallback_price=float(curr['close']),
        rsi14=float(curr['RSI_14']) if 'RSI_14' in curr.index else 50.0,
        sma50=float(curr['SMA_50']) if 'SMA_50' in curr.index else float(curr['close']),
    )
    current_price = _ov.price
    funding_rate  = _ov.funding_rate
    tvl_val       = _ov.tvl
    fng_val       = _ov.fng_val
    fng_state     = _ov.fng_state
    fng_source    = _ov.fng_source

    proxies = get_realtime_proxies(current_price, curr['close'])

    # 圖表切片
    try:
        mask     = (btc.index.date >= c_start) & (btc.index.date <= c_end)
        chart_df = btc.loc[mask]
        if chart_df.empty:
            chart_df = btc.tail(365)
    except Exception:
        chart_df = btc.tail(365)

# ==============================================================================
# 3. 頁面標題
# ==============================================================================
st.title("🦅 比特幣投資戰情室")

if _data_warnings:
    with st.expander(f"⚠️ {len(_data_warnings)} 個數據警告（不影響核心功能）", expanded=False):
        for w in _data_warnings:
            st.warning(w)

# ==============================================================================
# 4. 今日大盤速覽 (Global Overview Panel) — 每 60 秒 fragment 自動更新
# ==============================================================================
render_realtime_overview(
    prev_close=float(btc['close'].iloc[-2]) if len(btc) > 1 else float(curr['close']),
    fallback_price=float(curr['close']),
    rsi14=float(curr['RSI_14']) if 'RSI_14' in curr.index else 50.0,
    sma50=float(curr['SMA_50']) if 'SMA_50' in curr.index else float(curr['close']),
    ahr999=float(curr['AHR999']) if 'AHR999' in curr.index else math.nan,
)

# ==============================================================================
# 4b. 加密貨幣熱門新聞（速覽下方，全頁共用）
# ==============================================================================
render_news_panel()

# ==============================================================================
# 5. Tabs
# ==============================================================================
tab1, tab2, tab3, tab4 = st.tabs([
    "🧭 長週期羅盤 (Macro Compass)",
    "🌊 波段狙擊 (Swing Trading)",
    "💰 雙幣理財 (Dual Investment)",
    "⏳ 時光機回測 (Backtest)",
])

with tab1:
    tab1_handler.render(
        btc, chart_df, tvl_hist, stable_hist, fund_hist,
        curr, dxy, funding_rate, tvl_val,
        fng_val, fng_state, fng_source, proxies, realtime_data,
    )

with tab2:
    tab2_handler.render(
        btc, curr, funding_rate, proxies,
        open_interest=realtime_data.open_interest,
        open_interest_usd=realtime_data.open_interest_usd,
        oi_change_pct=realtime_data.oi_change_pct,
        current_price=current_price,
    )

with tab3:
    tab3_handler.render(btc, realtime_data)

with tab4:
    tab4_handler.render(btc)
