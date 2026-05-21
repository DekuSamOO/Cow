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
from service.mock import (
    get_mock_funding_rate,
    get_mock_tvl,
    calculate_fear_greed_proxy,
    get_realtime_proxies,
)

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

    _rt_price = rt.price
    current_price = _rt_price or fallback_price
    _price_source = rt.price_source or "歷史收盤"

    _funding_rate = (
        rt.funding_rate if rt.funding_rate is not None
        else get_mock_funding_rate()
    )
    _tvl_val = (
        rt.tvl if rt.tvl is not None
        else get_mock_tvl(current_price)
    )

    if rt.fng_value:
        _fng_val   = rt.fng_value
        _fng_state = rt.fng_class
        if "Greed" in _fng_state:
            _fng_state += " 🤑"
        elif "Fear" in _fng_state:
            _fng_state += " 😨"
        _fng_source = "Alternative.me"
    else:
        _fng_val    = calculate_fear_greed_proxy(rsi14, current_price, sma50)
        _fng_state  = "Proxy Mode"
        _fng_source = "Antigravity Proxy"

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

    _fr_delta = "🔥 多頭過熱" if _funding_rate > 0.03 else ("🟢 中性" if _funding_rate > 0 else "❄️ 空頭")
    _c3.metric(
        "💸 資金費率",
        f"{_funding_rate:.4f}%",
        _fr_delta,
        delta_color="inverse" if _funding_rate > 0.03 else "normal",
    )
    _c3.caption(f"來源：{rt.funding_rate_source or '模擬值'}")

    _tvl_display = f"${_tvl_val/1e9:.2f}B" if _tvl_val > 1e9 else f"${_tvl_val:.2f}M"
    _c4.metric("🏦 BTC 生態 TVL", _tvl_display, "↑ 鏈上活躍" if _tvl_val > 0 else "—")
    _c4.caption(f"來源：{rt.tvl_source or '模擬值'}")

    if not math.isnan(ahr999):
        _ahr_state = "🟢 抄底區" if ahr999 < 0.45 else ("🟡 合理區" if ahr999 < 1.2 else "🔴 高估區")
        _c5.metric("📐 AHR999", f"{ahr999:.3f}", _ahr_state)
    else:
        _c5.metric("📐 AHR999", "—", "計算中")
    _c5.caption("來源：歷史計算")

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
        realtime_data = {k: None for k in [
            'price', 'funding_rate', 'tvl', 'stablecoin_mcap', 'defi_yield',
            'fng_value', 'fng_class',
            'open_interest', 'open_interest_usd', 'oi_change_pct',
        ]}
        _data_warnings.append(f"即時數據載入失敗，使用模擬數據: {e}")

    curr          = btc.iloc[-1]
    current_price = float(realtime_data.get('price') or curr['close'])

    # Fallback 數值
    funding_rate = (
        realtime_data['funding_rate']
        if realtime_data['funding_rate'] is not None
        else get_mock_funding_rate()
    )
    tvl_val = (
        realtime_data['tvl']
        if realtime_data['tvl'] is not None
        else get_mock_tvl(current_price)
    )

    # 恐懼貪婪指數
    if realtime_data['fng_value']:
        fng_val   = realtime_data['fng_value']
        fng_state = realtime_data['fng_class']
        if "Greed" in fng_state:
            fng_state += " 🤑"
        elif "Fear" in fng_state:
            fng_state += " 😨"
        fng_source = "Alternative.me"
    else:
        fng_val    = calculate_fear_greed_proxy(
            float(curr['RSI_14']) if 'RSI_14' in curr.index else 50.0,
            current_price,
            float(curr['SMA_50']) if 'SMA_50' in curr.index else float(curr['close']),
        )
        fng_state  = "Proxy Mode"
        fng_source = "Antigravity Proxy"

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
        open_interest=realtime_data.get('open_interest'),
        open_interest_usd=realtime_data.get('open_interest_usd'),
        oi_change_pct=realtime_data.get('oi_change_pct'),
        current_price=current_price,
    )

with tab3:
    tab3_handler.render(btc, realtime_data)

with tab4:
    tab4_handler.render(btc)
