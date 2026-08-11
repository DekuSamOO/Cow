"""
Cache 一致性契約測試（P2-1）

CLAUDE.md 已記錄 cache 設計的兩個踩坑（**引標題不引序號，序號會隨增刪漂移**）：
  - 陷阱〈`@st.cache_data(ttl=60)` + `run_every=60` 衝突〉：fragment 與 TTL 同為 60 秒
           → 永遠命中快取
  - 〈service 層 fallback chain〉來源追蹤慣例：read_btc_15m 有 ttl=86400 快取，
           不可用於即時價格；get_latest_local_price 不帶 cache，供即時備援

本檔以契約方式鎖定這些設計決策，防止未來改動意外破壞。
"""

import pytest


def _is_cached(fn) -> bool:
    """判斷函數是否被 @st.cache_data 裝飾。

    Streamlit 的 cache_data 會把函數包成 CachedFunc，提供 .clear() method。
    """
    return hasattr(fn, "clear") and callable(getattr(fn, "clear", None))


# ──────────────────────────────────────────────────────────────────────────────
# service.realtime — 即時數據（**不可** 掛 cache，與 fragment 衝突）
# ──────────────────────────────────────────────────────────────────────────────

def test_fetch_realtime_data_must_not_be_cached():
    """fetch_realtime_data 不可掛 @st.cache_data。

    根因：app.py 用 @st.fragment(run_every=60) 每分鐘重跑 render_realtime_overview，
    若此函數也掛 cache_data(ttl=60)，會永遠命中快取、即時數據停止刷新。
    """
    from service.realtime import fetch_realtime_data
    assert not _is_cached(fetch_realtime_data), (
        "fetch_realtime_data 被加上 @st.cache_data — 會與 fragment 衝突，"
        "請查看 CLAUDE.md 陷阱〈@st.cache_data(ttl=60) + run_every=60 衝突〉"
    )


def test_get_latest_local_price_must_not_be_cached():
    """get_latest_local_price 不可掛 cache，供即時備援。

    根因：CLAUDE.md〈service 層 fallback chain〉來源追蹤慣例明確指出此函數設計為即時備援，
    不帶 @st.cache_data 才能在 Binance/Kraken 失敗時回傳本地 DB 最新一筆。
    """
    from service.local_db_reader import get_latest_local_price
    assert not _is_cached(get_latest_local_price), (
        "get_latest_local_price 被加上 cache，會破壞即時備援能力，"
        "請查看 CLAUDE.md〈service 層 fallback chain〉來源追蹤慣例"
    )


# ──────────────────────────────────────────────────────────────────────────────
# service.local_db_reader — 歷史數據（必須掛 cache，避免反覆讀 SQLite）
# ──────────────────────────────────────────────────────────────────────────────

def test_read_btc_15m_must_be_cached():
    """read_btc_15m 必須掛 @st.cache_data(ttl=86400)。

    每次重跑都重新合併多年 SQLite 太貴；cache 1 天避免重複工作。
    """
    from service.local_db_reader import read_btc_15m
    assert _is_cached(read_btc_15m), (
        "read_btc_15m 應掛 @st.cache_data(ttl=86400)，移除會嚴重拖慢頁面"
    )


def test_read_btc_daily_must_be_cached():
    """read_btc_daily 必須掛 @st.cache_data。"""
    from service.local_db_reader import read_btc_daily
    assert _is_cached(read_btc_daily), (
        "read_btc_daily 應掛 @st.cache_data，移除會反覆 resample"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 資料正確性：collector 寫入後，下次呼叫能讀到最新資料
# ──────────────────────────────────────────────────────────────────────────────

def test_get_latest_local_price_returns_fresh_value(tmp_path, monkeypatch):
    """模擬：寫入新一筆 K 線後，get_latest_local_price 應立即回傳新值。

    這個測試驗證該函數沒有任何 module-level 快取，
    符合「即時備援」的設計契約。
    """
    import sqlite3
    from service import local_db_reader

    # 建立假的 db/ 目錄與單一年度 SQLite
    fake_db_dir = tmp_path / "db"
    fake_db_dir.mkdir()
    year = 2026
    db_path = fake_db_dir / f"btcusdt_15m_{year}.db"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE klines (
            open_time INTEGER PRIMARY KEY,
            open REAL, high REAL, low REAL, close REAL, volume REAL
        )
    """)
    conn.execute(
        "INSERT INTO klines VALUES (?, ?, ?, ?, ?, ?)",
        (1_700_000_000_000, 50000, 50500, 49500, 50100, 100.0),
    )
    conn.commit()
    conn.close()

    # 暫時改寫 DB_DIR 指向假目錄
    monkeypatch.setattr(local_db_reader, "DB_DIR", str(fake_db_dir))

    first = local_db_reader.get_latest_local_price()
    assert first == 50100, f"首次讀取應為 50100，實得 {first}"

    # 插入更新的一筆（更大的 open_time，更新的價格）
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO klines VALUES (?, ?, ?, ?, ?, ?)",
        (1_700_000_900_000, 50100, 51000, 50000, 50800, 120.0),
    )
    conn.commit()
    conn.close()

    second = local_db_reader.get_latest_local_price()
    assert second == 50800, (
        f"資料庫已更新但讀取仍為 {second}，"
        "代表函數意外被加上 cache，違反即時備援設計契約"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
