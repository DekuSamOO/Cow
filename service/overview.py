"""
service/overview.py
大盤速覽指標的降級解析 — 單一事實來源

背景：app.py 的即時速覽在「主流程」與「@st.fragment 自動更新」兩處，
原本各自重複一份「RealtimeData 為 None 時 fallback 到 mock / proxy」的邏輯，
改一處易漏另一處。此模組抽成單一 helper，兩處共用。

注意：回傳 dataclass 僅在呼叫端「函式體內」使用，不作為 @st.fragment 的「傳入參數」，
故不違反 fragment 只能接純量的限制（CLAUDE.md 陷阱 #1）。
"""
from dataclasses import dataclass
from typing import Optional

from service.mock import (
    get_mock_funding_rate,
    get_mock_tvl,
    calculate_fear_greed_proxy,
)


@dataclass
class OverviewMetrics:
    price: float
    price_source: str
    funding_rate: float
    funding_source: str
    tvl: float
    tvl_source: str
    fng_val: float
    fng_state: str
    fng_source: str
    stablecoin_mcap: Optional[float]
    funding_is_real: bool = True   # False = 即時抓取失敗、目前為模擬值
    tvl_is_real: bool = True


def resolve_overview_metrics(rt, *, fallback_price: float, rsi14: float, sma50: float) -> OverviewMetrics:
    """將 RealtimeData 解析為速覽所需指標，缺值時降級至 mock / proxy。

    rt：RealtimeData（duck-typed，避免循環 import）
    fallback_price：歷史收盤，作為即時價格缺漏時的備援
    rsi14 / sma50：恐懼貪婪指數 proxy 計算所需（即時 FNG 缺漏時）
    """
    price = rt.price or fallback_price
    price_source = rt.price_source or "歷史收盤"

    funding_is_real = rt.funding_rate is not None
    funding_rate = rt.funding_rate if funding_is_real else get_mock_funding_rate()
    funding_source = rt.funding_rate_source or "模擬值"

    tvl_is_real = rt.tvl is not None
    tvl = rt.tvl if tvl_is_real else get_mock_tvl(price)
    tvl_source = rt.tvl_source or "模擬值"

    if rt.fng_value:
        fng_val = rt.fng_value
        fng_state = rt.fng_class or ""
        if "Greed" in fng_state:
            fng_state += " 🤑"
        elif "Fear" in fng_state:
            fng_state += " 😨"
        fng_source = "Alternative.me"
    else:
        fng_val = calculate_fear_greed_proxy(rsi14, price, sma50)
        fng_state = "Proxy Mode"
        fng_source = "Antigravity Proxy"

    return OverviewMetrics(
        price=price, price_source=price_source,
        funding_rate=funding_rate, funding_source=funding_source,
        tvl=tvl, tvl_source=tvl_source,
        fng_val=fng_val, fng_state=fng_state, fng_source=fng_source,
        stablecoin_mcap=rt.stablecoin_mcap,
        funding_is_real=funding_is_real, tvl_is_real=tvl_is_real,
    )
