"""
service/realtime.py
即時數據服務 — 價格、資金費率、恐懼貪婪指數、未平倉量 (Open Interest)
TTL=60s，每分鐘刷新

[Task #1] SSL 繞過：企業網路常以中間人憑證攔截 HTTPS 流量，
導致 requests 驗證失敗。透過以下兩步解決：
  1. urllib3.disable_warnings()  — 靜默 InsecureRequestWarning 警告
  2. safe_get(..., verify=SSL_VERIFY) — 動態 SSL 驗證
  本地開發 SSL_VERIFY=False，雲端部署 SSL_VERIFY=True（透過 config.py 控制）

[OI Data] 未平倉量 (Open Interest):
  直接呼叫 Binance API 抓取 BTC/USDT 永續合約的即時未平倉量。
  同時計算與上一次快取值的變化百分比，作為趨勢延續的輔助判斷指標。
  - OI 上升 + 價格上漲 → 強勢趨勢延續（多頭建倉）
  - OI 上升 + 價格下跌 → 空頭主導建倉（趨勢可能反轉）
  - OI 下降           → 持倉平倉，趨勢動能衰竭
"""
import random
import logging
import requests
from core.http_client import safe_get, safe_post
import urllib3   # [Task #1] 引入 urllib3 以關閉 SSL 警告
import streamlit as st
from dataclasses import dataclass
from typing import Optional

# 從集中設定檔讀取環境參數（SSL 驗證旗標）
from config import SSL_VERIFY
from service.local_db_reader import get_latest_local_price

logger = logging.getLogger(__name__)

# [Task #1] 動態 SSL：本地開發環境才關閉警告；雲端 SSL_VERIFY=True 保持正常
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class RealtimeData:
    price: Optional[float] = None
    price_source: Optional[str] = None
    funding_rate: Optional[float] = None
    funding_rate_source: Optional[str] = None
    tvl: Optional[float] = None
    tvl_source: Optional[str] = None
    stablecoin_mcap: Optional[float] = None
    defi_yield: Optional[float] = None
    fng_value: Optional[int] = None
    fng_class: Optional[str] = None
    open_interest: Optional[float] = None
    open_interest_usd: Optional[float] = None
    oi_change_pct: Optional[float] = None
    is_mocked: bool = False

def fetch_realtime_data() -> RealtimeData:
    """
    即時抓取:
    1. Binance 現貨/期貨價格、資金費率、未平倉量 OI (改用直接 requests 繞過 SSL 阻擋)
    2. DeFiLlama TVL & 穩定幣市值
    3. Alternative.me 恐懼貪婪指數
    返回: RealtimeData
    """
    data = RealtimeData()

    # 建立偽裝的 Headers，避免被幣安等 API 的反爬蟲機制 (WAF) 阻擋
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 1. Binance 數據 (棄用 ccxt，改用 requests 以強制套用 verify=SSL_VERIFY 與 headers)
    try:
        # 取得現貨最新價格
        r_price = safe_get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            timeout=3,
            verify=SSL_VERIFY,
            headers=headers  # 加入偽裝 Header
        )
        if r_price.status_code == 200:
            data.price = float(r_price.json()['price'])
            data.price_source = "Binance"

        # 取得期貨市場數據 (資金費率 & 未平倉量)
        try:
            # 資金費率 (Premium Index 端點)
            r_fr = safe_get(
                "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", 
                timeout=5, 
                verify=SSL_VERIFY,
                headers=headers  # 加入偽裝 Header
            )
            if r_fr.status_code == 200:
                # API 回傳的 lastFundingRate 是小數 (例如 0.000012 代表 0.0012%)
                data.funding_rate = float(r_fr.json()['lastFundingRate']) * 100
                data.funding_rate_source = "Binance"

            # 未平倉量 (Open Interest 端點)
            r_oi = safe_get(
                "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", 
                timeout=5, 
                verify=SSL_VERIFY,
                headers=headers  # 加入偽裝 Header
            )
            if r_oi.status_code == 200:
                current_oi = float(r_oi.json()['openInterest'])   # U本位 (BTC)

                # 加總幣本位 (COIN-M BTCUSD_PERP，張數×$100/價)，與 service/market_snapshot
                # 同源算法，得 Binance BTC 永續總 OI（已對 CoinGecko derivatives 交叉驗證）
                try:
                    r_coin = safe_get(
                        "https://dapi.binance.com/dapi/v1/openInterest?symbol=BTCUSD_PERP",
                        timeout=5, verify=SSL_VERIFY, headers=headers
                    )
                    if r_coin.status_code == 200 and data.price:
                        contracts = float(r_coin.json()['openInterest'])
                        current_oi += (contracts * 100) / data.price
                except Exception as e:
                    logger.warning(f"COIN-M OI fetch error: {e}")

                data.open_interest = current_oi

                # 以美元計算（顆數 × 現價），單位：億 USD
                if data.price:
                    data.open_interest_usd = (current_oi * data.price) / 1e8

                # 計算 60s 變化率
                try:
                    prev_oi = st.session_state.get('_prev_oi', None)
                    if prev_oi is not None and prev_oi > 0:
                        data.oi_change_pct = (current_oi / prev_oi - 1) * 100
                    st.session_state['_prev_oi'] = current_oi
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"Binance futures direct API error (OI/funding): {e}")

    except Exception as e:
        logger.warning(f"Binance spot direct API error: {e}")

    # 1d. Bybit 資金費率備援（Binance fapi 遭封鎖時）
    if data.funding_rate is None:
        try:
            r_bybit = safe_get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": "BTCUSDT"},
                timeout=5,
                verify=SSL_VERIFY,
                headers=headers,
            )
            if r_bybit.status_code == 200:
                result = r_bybit.json()
                if result.get("retCode") == 0:
                    for item in result.get("result", {}).get("list", []):
                        if item.get("symbol") == "BTCUSDT":
                            data.funding_rate = float(item['fundingRate']) * 100
                            data.funding_rate_source = "Bybit"
                            logger.info("[Realtime] Bybit 備援資金費率成功")
                            break
        except Exception as e:
            logger.warning(f"Bybit funding rate error: {e}")

    # 1e. OKX 資金費率備援（Bybit 也失敗時）
    if data.funding_rate is None:
        try:
            r_okx = safe_get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": "BTC-USDT-SWAP"},
                timeout=5,
                verify=SSL_VERIFY,
                headers=headers,
            )
            if r_okx.status_code == 200:
                okx_data = r_okx.json()
                if okx_data.get("code") == "0" and okx_data.get("data"):
                    data.funding_rate = float(okx_data['data'][0]['fundingRate']) * 100
                    data.funding_rate_source = "OKX"
                    logger.info("[Realtime] OKX 備援資金費率成功")
        except Exception as e:
            logger.warning(f"OKX funding rate error: {e}")

    # 1b. Kraken 現貨備援（與 market_data.py 同源，企業防火牆較少封鎖）
    if data.price is None:
        try:
            r_kr = safe_get(
                "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
                timeout=5,
                verify=SSL_VERIFY,
                headers=headers,
            )
            if r_kr.status_code == 200:
                result = r_kr.json().get('result', {})
                pair_data = result.get('XXBTZUSD') or result.get('XBTUSD')
                if pair_data:
                    data.price = float(pair_data['c'][0])
                    data.price_source = "Kraken"
                    logger.info("[Realtime] Kraken 備援價格成功")
        except Exception as e:
            logger.warning(f"Kraken realtime price error: {e}")

    # 1c. 本地 15m DB 備援（完全離線，collector 有在跑時最新至 15 分鐘內）
    if data.price is None:
        try:
            local_p = get_latest_local_price()
            if local_p:
                data.price = local_p
                data.price_source = "本地DB"
                logger.info("[Realtime] 本地 DB 備援價格成功")
        except Exception as e:
            logger.warning(f"Local DB price error: {e}")

    # 2. DeFiLlama
    try:
        r = safe_get(
            "https://api.llama.fi/v2/chains", 
            timeout=5, 
            verify=SSL_VERIFY,
            headers=headers
        )
        if r.status_code == 200:
            for c in r.json():
                if c['name'] == 'Bitcoin':
                    data.tvl = c['tvl'] / 1e9
                    data.tvl_source = "DeFiLlama"
                    break

        r2 = safe_get(
            "https://stablecoins.llama.fi/stablecoins?includePrices=true",
            timeout=5,
            verify=SSL_VERIFY,
            headers=headers
        )
        if r2.status_code == 200:
            total = sum(
                s.get('circulating', {}).get('peggedUSD', 0)
                for s in r2.json().get('peggedAssets', [])
                if s['symbol'] in ['USDT', 'USDC', 'DAI', 'FDUSD', 'USDD']
            )
            data.stablecoin_mcap = total / 1e9

        data.defi_yield = 5.0 + random.uniform(-0.5, 0.5)  # 模擬值（無公開即時 API）

    except Exception as e:
        logger.warning(f"DeFiLlama error: {e}")

    # 3. Fear & Greed
    try:
        r = safe_get(
            "https://api.alternative.me/fng/", 
            timeout=5, 
            verify=SSL_VERIFY,
            headers=headers
        )
        if r.status_code == 200:
            item = r.json()['data'][0]
            data.fng_value = int(item['value'])
            data.fng_class = item['value_classification']
    except Exception as e:
        logger.warning(f"F&G error: {e}")

    return data