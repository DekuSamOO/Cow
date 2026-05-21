import logging
from typing import Callable, List, Tuple, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class FallbackResult:
    data: Any
    source_used: str
    attempts: int

class FallbackChain:
    """
    統一管理資料源的 Fallback 鏈。
    依序嘗試各個資料源（fetcher_fn），只要某個資料源回傳非空結果即停止並回傳。
    若全部失敗，則回傳最後一個資料源的結果（或預設值）。
    """
    def __init__(self, name: str, chain: List[Tuple[str, Callable[..., Any]]]):
        """
        :param name: 這條 Fallback 鏈的名稱（供 log 辨識，例如 "BTC_History"）
        :param chain: 一個 list，每個元素為 (資料源名稱, 取得函數)
        """
        self.name = name
        self.chain = chain

    def fetch(self, *args, **kwargs) -> FallbackResult:
        """
        依序執行 chain 中的函數。
        若函數沒有拋出例外，且有回傳非空資料（例如 DataFrame.empty == False 或不為 None），則視為成功。
        """
        attempts = 0
        last_data = None
        last_source = "None"
        
        for source_name, fetcher_fn in self.chain:
            attempts += 1
            try:
                data = fetcher_fn(*args, **kwargs)
                
                # 判斷資料是否為空 (支援 pandas DataFrame)
                is_empty = False
                if data is None:
                    is_empty = True
                elif hasattr(data, 'empty'):
                    is_empty = data.empty
                elif isinstance(data, (list, dict, str)) and len(data) == 0:
                    is_empty = True

                if not is_empty:
                    logger.info(f"[{self.name}] 成功從 '{source_name}' 取得資料")
                    return FallbackResult(data=data, source_used=source_name, attempts=attempts)
                else:
                    logger.warning(f"[{self.name}] '{source_name}' 回傳空資料，嘗試下一個來源...")
                    last_data = data
                    last_source = source_name
            except Exception as e:
                logger.error(f"[{self.name}] '{source_name}' 發生例外: {e}，嘗試下一個來源...")

        logger.warning(f"[{self.name}] 所有資料源皆失敗或回傳空值，使用最後一個嘗試的結果。")
        return FallbackResult(data=last_data, source_used=last_source, attempts=attempts)

# ── 預先定義好的 Fallback 鏈 (供各 service 使用) ──────────────────────────

def build_btc_history_chain(session) -> FallbackChain:
    from service.market_data import (
        read_btc_daily,
        fetch_binance_daily,
        fetch_kraken_daily,
        fetch_cryptocompare_daily,
        has_local_data
    )
    import yfinance as yf
    
    def fetch_local(start_date):
        if has_local_data():
            return read_btc_daily(start_date=start_date)
        return None
        
    def fetch_yf(start_date):
        df = yf.download("BTC-USD", start=start_date, interval="1d", progress=False, session=session)
        if not df.empty:
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        return df

    return FallbackChain("BTC_History", [
        ("Local SQLite", fetch_local),
        ("Yahoo Finance", fetch_yf),
        ("Binance REST", fetch_binance_daily),
        ("Kraken", fetch_kraken_daily),
        ("CryptoCompare", fetch_cryptocompare_daily)
    ])

def build_btc_stitch_chain(session) -> FallbackChain:
    from service.market_data import (
        fetch_kraken_daily,
        fetch_cryptocompare_daily
    )
    import yfinance as yf
    
    def fetch_yf(start_date):
        df = yf.download("BTC-USD", start=start_date, interval="1d", progress=False, session=session)
        if not df.empty:
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        return df

    return FallbackChain("BTC_Stitch", [
        ("Yahoo Finance", fetch_yf),
        ("Kraken", fetch_kraken_daily),
        ("CryptoCompare", fetch_cryptocompare_daily)
    ])
