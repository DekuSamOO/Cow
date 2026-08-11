"""
tests/test_market_data.py
BTC 市場數據診斷測試

執行方式:
  pytest tests/test_market_data.py -v -s

功能:
  - 確認 yfinance 版本與下載行為
  - 確認 SQLite 讀寫往返（含大小寫 index 欄位問題）
  - 確認 data_manager._df_from_sqlite / _df_to_sqlite 正常工作
  - 確認白名單驗證防止非法表格名稱
  - 確認完整 fetch_market_data() 流程（需網路）
"""
import sys
import os
import tempfile
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 確保可以 import 專案模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────
# Section 1: yfinance 診斷
# ─────────────────────────────────────────────

def test_yfinance_version():
    """印出 yfinance 版本，確認已安裝。"""
    import yfinance as yf
    version = getattr(yf, '__version__', 'unknown')
    print(f"\n[yfinance] version = {version}")
    assert yf is not None, "yfinance 未安裝"


@pytest.mark.yahoo
def test_yfinance_download_short_range():
    """
    下載近 30 天 BTC 數據，驗證:
    - 回傳非空 DataFrame
    - 包含 close / open / high / low / volume 欄位（或 MultiIndex 形式）
    - index 為 DatetimeIndex
    """
    import yfinance as yf
    start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    import requests
    session = requests.Session()
    session.verify = False

    df = yf.download("BTC-USD", start=start, interval="1d",
                     progress=False, auto_adjust=True, session=session)

    print(f"  shape       = {df.shape}")
    print(f"  columns     = {list(df.columns)}")
    print(f"  index dtype = {df.index.dtype}")
    print(f"  index name  = {df.index.name}")
    print(f"  tz-aware    = {df.index.tz is not None}")
    print(f"  empty       = {df.empty}")
    if not df.empty:
        print(f"  head:\n{df.head(2)}")

    assert not df.empty, (
        "yf.download() 回傳空 DataFrame！\n"
        "可能原因: 網路問題 / Yahoo Finance 封鎖此 IP / yfinance API 變更"
    )


@pytest.mark.yahoo
def test_yfinance_ticker_history():
    """
    使用 Ticker().history() 備援下載，驗證欄位格式。
    """
    import yfinance as yf
    start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    import requests
    session = requests.Session()
    session.verify = False

    ticker_obj = yf.Ticker("BTC-USD", session=session)
    df = ticker_obj.history(start=start, interval="1d", auto_adjust=True)

    print(f"  shape       = {df.shape}")
    print(f"  columns     = {list(df.columns)}")
    print(f"  index name  = {df.index.name}")
    print(f"  tz-aware    = {df.index.tz is not None}")
    print(f"  empty       = {df.empty}")

    assert not df.empty, (
        "yf.Ticker.history() 也回傳空 DataFrame！\n"
        "yfinance 在此環境可能無法連線 Yahoo Finance。"
    )


# ─────────────────────────────────────────────
# Section 2: SQLite 讀寫往返
# ─────────────────────────────────────────────

def _patch_db_path(tmp_dir: str):
    """
    將 data_manager 的 DB_PATH 改指向暫存目錄，
    避免污染正式數據庫。
    """
    import service.historical_data_manager as data_manager
    orig_path = data_manager.DB_PATH
    data_manager.DB_PATH = os.path.join(tmp_dir, "test_cow.db")
    return orig_path


def _restore_db_path(orig_path: str):
    import service.historical_data_manager as data_manager
    data_manager.DB_PATH = orig_path


def test_sqlite_write_read_roundtrip():
    """
    驗證 _df_to_sqlite → _df_from_sqlite 往返一致，
    特別測試 yfinance 常見的 'Date'（大寫）index 名稱。
    """
    import service.historical_data_manager as data_manager

    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_db_path(tmp)
        try:
            # 建立模擬 BTC DataFrame，index 名稱為 'Date'（yfinance 實際返回值）
            idx = pd.date_range('2025-01-01', periods=5, name='Date')
            df_write = pd.DataFrame({
                'open':   [10.0, 11.0, 12.0, 13.0, 14.0],
                'close':  [10.5, 11.5, 12.5, 13.5, 14.5],
                'high':   [11.0, 12.0, 13.0, 14.0, 15.0],
                'low':    [ 9.5, 10.5, 11.5, 12.5, 13.5],
                'volume': [100.0, 200.0, 300.0, 400.0, 500.0],
            }, index=idx)

            print(f"\n[test] 寫入 DataFrame, index.name='{df_write.index.name}', shape={df_write.shape}")
            data_manager._df_to_sqlite(df_write, 'btc_history')

            df_read = data_manager._df_from_sqlite('btc_history')
            print(f"[test] 讀回 DataFrame, shape={df_read.shape}, index.name='{df_read.index.name}'")
            print(f"[test] 讀回欄位: {list(df_read.columns)}")

            assert not df_read.empty, (
                "_df_from_sqlite 回傳空 DataFrame！\n"
                "很可能是 SQLite 欄位大小寫問題（'Date' vs 'date'）。"
            )
            assert len(df_read) == len(df_write), \
                f"資料列數不符: 寫入 {len(df_write)}, 讀回 {len(df_read)}"
            assert 'close' in df_read.columns, \
                f"讀回 DataFrame 缺少 'close' 欄位，欄位列表: {list(df_read.columns)}"
            pd.testing.assert_index_equal(
                df_read.index.normalize(),
                df_write.index.normalize(),
                check_names=False,
            )
            print("[test] SQLite 讀寫往返 ✅ 通過")
        finally:
            _restore_db_path(orig)


def test_sqlite_preserves_data_column_casing():
    """
    [C-17] _df_from_sqlite 不應把資料欄位（如 fundingRate）強制轉小寫，
    只應正規化 index 欄位大小寫。
    """
    import service.historical_data_manager as data_manager

    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_db_path(tmp)
        try:
            idx = pd.date_range('2025-01-01', periods=3, name='date')
            df_write = pd.DataFrame({'fundingRate': [0.01, 0.02, 0.03]}, index=idx)
            data_manager._df_to_sqlite(df_write, 'funding_history')

            df_read = data_manager._df_from_sqlite('funding_history')
            print(f"\n[test] 讀回欄位: {list(df_read.columns)}")
            assert 'fundingRate' in df_read.columns, (
                f"資料欄位大小寫被錯誤轉換，讀回欄位: {list(df_read.columns)}"
            )
            print("[test] 資料欄位大小寫保留 ✅ 通過")
        finally:
            _restore_db_path(orig)


def test_sqlite_incremental_concat_no_duplicate_column_crash():
    """
    [C-17] 重現並驗證修復：existing_df（讀回）與新 fetched_df 兩者的
    fundingRate 欄位大小寫必須一致，concat 後寫回不應觸發 SQLite
    'duplicate column name' 錯誤。
    """
    import service.historical_data_manager as data_manager

    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_db_path(tmp)
        try:
            idx1 = pd.date_range('2025-01-01', periods=3, name='date')
            df_write = pd.DataFrame({'fundingRate': [0.01, 0.02, 0.03]}, index=idx1)
            data_manager._df_to_sqlite(df_write, 'funding_history')

            existing_df = data_manager._df_from_sqlite('funding_history')

            idx2 = pd.date_range('2025-01-04', periods=2, name='date')
            fetched_df = pd.DataFrame({'fundingRate': [0.04, 0.05]}, index=idx2)

            full_df = pd.concat([existing_df, fetched_df])
            full_df = full_df[~full_df.index.duplicated(keep='last')]
            full_df.sort_index(inplace=True)

            assert list(full_df.columns) == ['fundingRate'], (
                f"concat 產生了大小寫分裂的重複欄位: {list(full_df.columns)}"
            )

            # 第二次寫回不應拋出 sqlite3.OperationalError: duplicate column name
            data_manager._df_to_sqlite(full_df, 'funding_history')

            df_read_back = data_manager._df_from_sqlite('funding_history')
            assert len(df_read_back) == 5
            print("[test] 增量 concat 無重複欄位、寫回無崩潰 ✅ 通過")
        finally:
            _restore_db_path(orig)


def test_sqlite_empty_on_missing_valid_table():
    """確認有效表格名稱但尚未建立時，_df_from_sqlite 回傳空 DataFrame。"""
    import service.historical_data_manager as data_manager

    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_db_path(tmp)
        try:
            result = data_manager._df_from_sqlite('btc_history')
            print(f"\n[test] 不存在的有效表格 → empty={result.empty}")
            assert result.empty, "尚未建立的表格應回傳空 DataFrame"
            print("[test] 缺失表格行為 ✅ 正確")
        finally:
            _restore_db_path(orig)


def test_sqlite_whitelist_rejects_invalid_table():
    """確認白名單驗證拒絕非法表格名稱，防止 SQL injection。"""
    import service.historical_data_manager as data_manager

    with pytest.raises(ValueError, match="不允許的表格名稱"):
        data_manager._df_from_sqlite('nonexistent_table')
    print("\n[test] 白名單驗證 ✅ 正確拒絕非法表格名稱")


# ─────────────────────────────────────────────
# Section 3: 端對端 fetch_market_data（標記網路依賴）
# ─────────────────────────────────────────────

@pytest.mark.network
def test_fetch_binance_daily_recent():
    """
    測試 fetch_binance_daily()（需網路連線）。
    驗證回傳 DataFrame 含有必要欄位且 index 無時區。
    """
    from service.market_data import fetch_binance_daily

    start = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    print(f"\n[test] fetch_binance_daily(start={start})")
    df = fetch_binance_daily(start)

    print(f"  shape   = {df.shape}")
    print(f"  columns = {list(df.columns)}")
    if not df.empty:
        print(f"  head:\n{df.head(2)}")

    if df.empty:
        pytest.skip("Binance API 在此環境無法連線（451 封鎖或網路問題）")

    required = {'close', 'open', 'high', 'low', 'volume'}
    missing = required - set(df.columns)
    assert not missing, f"缺少必要欄位: {missing}，實際欄位: {list(df.columns)}"
    assert df.index.tz is None, f"index 帶有時區 {df.index.tz}，應為 tz-naive"
    print("[test] fetch_binance_daily ✅ 通過")
