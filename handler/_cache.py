import streamlit as st
from typing import Callable, Any
from functools import wraps
import hashlib
import pandas as pd
from typing import Callable, Any
from functools import wraps

class CowCacheNamespace:
    """
    統一管理 Streamlit Session State 的命名空間，
    避免直接使用散落的字串鍵名導致衝突或難以追蹤。
    """
    def __init__(self, prefix: str):
        self.prefix = prefix

    def get_key(self, suffix: str) -> str:
        return f"{self.prefix}_{suffix}"

    def get(self, suffix: str, default: Any = None) -> Any:
        key = self.get_key(suffix)
        return st.session_state.get(key, default)

    def set(self, suffix: str, value: Any):
        key = self.get_key(suffix)
        st.session_state[key] = value

    def contains(self, suffix: str) -> bool:
        return self.get_key(suffix) in st.session_state


def cached_figure(namespace: CowCacheNamespace, key_fn: Callable[..., str]):
    """
    自動檢查並寫入 session_state 的 Plotly 圖表快取裝飾器。
    
    :param namespace: 隸屬的 CowCacheNamespace 實例
    :param key_fn: 一個函數，根據裝飾函數的參數產生對應的 cache_key (不含 prefix)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            suffix = key_fn(*args, **kwargs)
            if namespace.contains(suffix):
                return namespace.get(suffix)
            
            fig = func(*args, **kwargs)
            namespace.set(suffix, fig)
            return fig
        return wrapper
    return decorator

def make_mc_cache_key(chart_df: pd.DataFrame, tvl_hist: pd.DataFrame, stable_hist: pd.DataFrame, fund_hist: pd.DataFrame) -> str:
    parts = [
        str(chart_df.index[-1])    if not chart_df.empty    else "empty",
        str(len(chart_df)),
        str(tvl_hist.index[-1])    if not tvl_hist.empty    else "empty",
        str(stable_hist.index[-1]) if not stable_hist.empty else "empty",
        str(fund_hist.index[-1])   if not fund_hist.empty   else "empty",
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]

def make_bb_cache_key(btc: pd.DataFrame) -> str:
    last_idx = str(btc.index[-1]) if not btc.empty else "empty"
    return hashlib.md5(f"{last_idx}|{len(btc)}".encode()).hexdigest()[:16]

macro_compass_cache = CowCacheNamespace("tab_macro_compass")
