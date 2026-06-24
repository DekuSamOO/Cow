"""
collector/btc_price_collector.py
本地端 BTC/USDT 15 分鐘 K 線收集器

用法：
  python collector/btc_price_collector.py          # 更新所有年份（增量）
  python collector/btc_price_collector.py --year 2021       # 只更新特定年份
  python collector/btc_price_collector.py --from-year 2017  # 從指定年份到現在
  python collector/btc_price_collector.py --push            # 收集完後自動 git push

數據源：
  - Binance  REST API（2017-08-17 起，BTCUSDT 15m，最高流動性）
  - Kraken   REST API（2013 起，XBTUSD 15m，無地理封鎖，填補 Binance 前空白）

儲存結構：
  db/
    btcusdt_15m_2013.db
    btcusdt_15m_2014.db
    ...
    btcusdt_15m_2026.db

每個 SQLite 檔案包含 klines 表：
  open_time INTEGER PRIMARY KEY  -- Unix 毫秒時間戳
  open      REAL
  high      REAL
  low       REAL
  close     REAL
  volume    REAL
"""

import os
import sys
import time
import sqlite3
import argparse
import subprocess
from datetime import datetime, timezone

import requests

# ── 路徑設定（須在 import core/* 之前；使直接 `python collector/btc_price_collector.py`
#    執行時——script 目錄才進 sys.path、repo root 不在——也能 import core/config）──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = os.path.dirname(_SCRIPT_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
DB_DIR      = os.path.join(_REPO_ROOT, "db")

from core.http_client import safe_get, safe_post

# ── 常數 ──────────────────────────────────────────────────────────────────────
INTERVAL_MS       = 15 * 60 * 1000          # 15 分鐘（毫秒）
BINANCE_START_STR = "2017-08-17"            # Binance BTCUSDT 最早可用日期
BINANCE_START_MS  = int(
    datetime(2017, 8, 17, tzinfo=timezone.utc).timestamp() * 1000
)
KRAKEN_START_YEAR = 2013                    # Kraken XBTUSD 最早年份


# ══════════════════════════════════════════════════════════════════════════════
# SQLite 工具
# ══════════════════════════════════════════════════════════════════════════════

def get_db_path(year: int) -> str:
    return os.path.join(DB_DIR, f"btcusdt_15m_{year}.db")


def init_db(year: int) -> sqlite3.Connection:
    """建立（或開啟）年度 SQLite 並確保 klines 表存在。"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(get_db_path(year))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            open_time INTEGER PRIMARY KEY,
            open      REAL NOT NULL,
            high      REAL NOT NULL,
            low       REAL NOT NULL,
            close     REAL NOT NULL,
            volume    REAL NOT NULL
        )
    """)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.commit()
    return conn


def get_last_open_time(year: int):
    """回傳該年 DB 中最新一根 K 線的 open_time（ms），若 DB 不存在回傳 None。"""
    db_path = get_db_path(year)
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT MAX(open_time) FROM klines").fetchone()
    conn.close()
    return row[0] if row else None


def get_row_count(year: int) -> int:
    db_path = get_db_path(year)
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
    conn.close()
    return count


def insert_rows(conn: sqlite3.Connection, rows: list) -> int:
    """批次寫入，重複 open_time 以新資料覆蓋（REPLACE）。"""
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?)", rows
    )
    conn.commit()
    return len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Binance 15m 抓取
# ══════════════════════════════════════════════════════════════════════════════

def _binance_klines(start_ms: int, end_ms: int) -> list:
    """
    從 Binance REST API 抓取 BTCUSDT 15m K 線。
    - 每次最多 1000 筆，自動分頁
    - 含指數退避重試（最多 4 次）
    回傳 list of (open_time_ms, open, high, low, close, volume)
    """
    url     = "https://api.binance.com/api/v3/klines"
    all_rows = []
    current  = start_ms

    while current < end_ms:
        for attempt in range(4):
            try:
                resp = safe_get(
                    url,
                    params={
                        "symbol":    "BTCUSDT",
                        "interval":  "15m",
                        "startTime": current,
                        "endTime":   end_ms,
                        "limit":     1000,
                    },
                    timeout=30,
                    verify=False  # 關閉 SSL 驗證，避免公司網路環境攔截導致抓取失敗
                )
                if resp.status_code == 451:
                    raise RuntimeError("Binance 451 地理封鎖，請改用 VPN 或切換 Kraken 模式")
                resp.raise_for_status()
                batch = resp.json()
                break
            except RuntimeError:
                raise
            except Exception as exc:
                if attempt == 3:
                    raise
                wait = 2 ** attempt
                print(f"    [Binance] 重試 {attempt+1}/3，等待 {wait}s… ({exc})")
                time.sleep(wait)

        if not batch:
            break

        rows = [
            (k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            for k in batch
        ]
        all_rows.extend(rows)

        last_open_time = batch[-1][0]
        current = last_open_time + INTERVAL_MS

        if len(batch) < 1000:
            break

        time.sleep(0.15)   # 尊重 Binance 1200 req/min 速率限制

    return all_rows


# ══════════════════════════════════════════════════════════════════════════════
# Kraken 15m 抓取
# ══════════════════════════════════════════════════════════════════════════════

def _kraken_klines(start_ms: int, end_ms: int) -> list:
    """
    從 Kraken REST API 抓取 XBTUSD 15m K 線。
    - Kraken 用秒級 Unix timestamp，每頁最多 720 筆（= 7.5 天）
    - 含指數退避重試（最多 4 次）
    回傳 list of (open_time_ms, open, high, low, close, volume)
    """
    url      = "https://api.kraken.com/0/public/OHLC"
    all_rows = []
    since_s  = start_ms // 1000
    end_s    = end_ms   // 1000

    while since_s < end_s:
        for attempt in range(4):
            try:
                resp = safe_get(
                    url,
                    params={"pair": "XBTUSD", "interval": 15, "since": since_s},
                    timeout=30,
                    verify=False  # 關閉 SSL 驗證，避免公司網路環境攔截導致抓取失敗
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("error"):
                    raise RuntimeError(f"Kraken API 錯誤: {data['error']}")
                break
            except RuntimeError:
                raise
            except Exception as exc:
                if attempt == 3:
                    raise
                wait = 2 ** attempt
                print(f"    [Kraken] 重試 {attempt+1}/3，等待 {wait}s… ({exc})")
                time.sleep(wait)

        # Kraken OHLC 格式: [time, open, high, low, close, vwap, volume, count]
        candles = data.get("result", {}).get("XXBTZUSD", [])
        if not candles:
            break

        for c in candles:
            ts_s  = int(c[0])
            ts_ms = ts_s * 1000
            if ts_ms >= end_ms:
                break
            if ts_ms >= start_ms:
                all_rows.append((
                    ts_ms,
                    float(c[1]),   # open
                    float(c[2]),   # high
                    float(c[3]),   # low
                    float(c[4]),   # close
                    float(c[6]),   # volume（跳過 vwap）
                ))

        last_s = int(data.get("result", {}).get("last", 0))
        if last_s <= since_s:
            break
        since_s = last_s

        time.sleep(0.5)   # Kraken: ~1 req/sec 建議速率

    return all_rows


# ══════════════════════════════════════════════════════════════════════════════
# 年度收集主邏輯
# ══════════════════════════════════════════════════════════════════════════════

def collect_year(year: int) -> int:
    """
    收集指定年份的所有 15m K 線並存入 db/btcusdt_15m_{year}.db。
    支援增量更新（只抓上次存檔後的新資料）。
    回傳本次新增筆數。
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    year_start_ms = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    year_end_ms   = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    year_end_ms   = min(year_end_ms, now_ms)

    # 確定抓取起點
    last_ts = get_last_open_time(year)
    if last_ts:
        fetch_start_ms = last_ts + INTERVAL_MS
        start_label = datetime.fromtimestamp(
            fetch_start_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        print(f"  增量更新：從 {start_label} 開始")
    else:
        fetch_start_ms = year_start_ms
        print(f"  全量下載：從 {year}-01-01 開始")

    if fetch_start_ms >= year_end_ms:
        count = get_row_count(year)
        print(f"  ✓ {year} 年已是最新（共 {count:,} 筆）")
        return 0

    # 選擇數據源並抓取
    try:
        if fetch_start_ms >= BINANCE_START_MS:
            # 全段都在 Binance 範圍內
            print(f"  數據源：Binance")
            rows = _binance_klines(fetch_start_ms, year_end_ms)

        elif year_end_ms <= BINANCE_START_MS:
            # 全段都在 Binance 啟動前，使用 Kraken
            print(f"  數據源：Kraken")
            rows = _kraken_klines(fetch_start_ms, year_end_ms)

        else:
            # 跨越 Binance 啟動日（2017 年）：前段 Kraken，後段 Binance
            print(f"  數據源：Kraken（前段）+ Binance（後段）")
            kraken_rows  = _kraken_klines(fetch_start_ms, BINANCE_START_MS)
            binance_rows = _binance_klines(BINANCE_START_MS, year_end_ms)
            rows = kraken_rows + binance_rows
            print(f"    Kraken {len(kraken_rows):,} 筆 + Binance {len(binance_rows):,} 筆")

    except Exception as exc:
        print(f"  ✗ 抓取失敗：{exc}")
        return 0

    if not rows:
        print(f"  ⚠ 無新數據")
        return 0

    # 寫入 DB
    conn = init_db(year)
    inserted = insert_rows(conn, rows)
    conn.close()

    total = get_row_count(year)
    first_dt = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    last_dt  = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"  ✓ 新增 {inserted:,} 筆（{first_dt} ~ {last_dt}）｜資料庫共 {total:,} 筆")

    return inserted


# ══════════════════════════════════════════════════════════════════════════════
# 市場快照（OI / 資金費率 / BTC.D）— 供相對高點累積歷史
# ══════════════════════════════════════════════════════════════════════════════

def snapshot_market_state():
    """
    快照當前 OI（U本位+幣本位加總）+ 資金費率 + BTC.D 到 db/market_snapshot.json，
    供 core/relative_high 的 OI 分位 / BTC.D 趨勢累積真實歷史。

    為何放在本地 collector：GitHub Actions（美國 IP）對 Binance OI 端點 geo-block（451），
    抓不到 OI；本機（公司/家用網路）的 fapi/dapi 可用。隨 collector 每次執行累積一筆，
    git_push 的 `git add db/` 會一併提交 market_snapshot.json。
    """
    try:
        from service.market_snapshot import append_daily_snapshot, backfill_oi_history
    except Exception as e:
        print(f"  ⚠ 無法載入 market_snapshot：{e}")
        return

    # 先回補缺漏日（週末關機沒跑 / 首次啟用）— Binance openInterestHist 近 30 天每日
    try:
        n = backfill_oi_history(30)
        if n:
            print(f"  ✓ 回補 {n} 天缺漏 OI 歷史（Binance 30 天每日）")
    except Exception as e:
        print(f"  ⚠ OI 回補失敗：{e}")

    fr = None
    try:
        r = safe_get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
                     timeout=10, verify=False)
        if r.status_code == 200:
            fr = float(r.json()["lastFundingRate"]) * 100   # % / 8h
    except Exception as e:
        print(f"  ⚠ 資金費率抓取失敗：{e}")

    btcd = None
    try:
        r = safe_get("https://api.coingecko.com/api/v3/global", timeout=10, verify=False)
        if r.status_code == 200:
            btcd = r.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
    except Exception as e:
        print(f"  ⚠ BTC.D 抓取失敗：{e}")

    try:
        snap = append_daily_snapshot(price=None, funding_rate=fr, btc_dominance=btcd)
    except Exception as e:
        print(f"  ✗ OI 快照寫入失敗：{e}")
        return

    if snap.get("oi_btc"):
        print(f"  ✓ OI 快照：{snap['oi_btc']:,.0f} BTC｜資費 {fr}%/8h｜BTC.D {btcd}")
    else:
        print(f"  ⚠ OI 快照未取得 OI（可能 Binance 封鎖此環境）")

    # ETF 流量快取刷新（Farside 對 datacenter IP 回 403，雲端/Actions 只能讀 repo 內快取
    # → 本機跑 collector 時順手強制刷新，git_push 的 `git add db/` 會一併 commit etf_flow.json）
    try:
        from service.etf_flow import fetch_etf_flow
        data = fetch_etf_flow(force=True)
        if data:
            print(f"  ✓ ETF 流量快取已刷新（{len(data)} 筆，最新 {max(data)}）")
        else:
            print("  ⚠ ETF 流量無資料（Farside 不可達且無既有快取）")
    except Exception as e:
        print(f"  ⚠ ETF 快取刷新失敗：{e}")


# ══════════════════════════════════════════════════════════════════════════════
# Git Push
# ══════════════════════════════════════════════════════════════════════════════

def git_push():
    """將 db/ 目錄下的 SQLite 檔案 commit 並推送到遠端。"""
    print("\n──────────────────────────────────────────")
    print("Git: 提交並推送至雲端...")

    try:
        # Stage db/ 資料夾
        subprocess.run(
            ["git", "-C", _REPO_ROOT, "add", "db/"],
            check=True, capture_output=True
        )

        # 確認是否有變更
        result = subprocess.run(
            ["git", "-C", _REPO_ROOT, "status", "--porcelain", "db/"],
            capture_output=True, text=True
        )
        if not result.stdout.strip():
            print("  ✓ 無新變更，跳過 commit")
            return

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"data: update BTC/USDT 15m klines [{now_str}]"
        subprocess.run(
            ["git", "-C", _REPO_ROOT, "commit", "-m", msg],
            check=True, capture_output=True
        )
        print(f"  ✓ Commit: {msg}")

        # 推送（帶指數退避重試）
        branch = subprocess.run(
            ["git", "-C", _REPO_ROOT, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True
        ).stdout.strip()

        for attempt in range(4):
            push_result = subprocess.run(
                ["git", "-C", _REPO_ROOT, "push", "-u", "origin", branch],
                capture_output=True, text=True
            )
            if push_result.returncode == 0:
                print(f"  ✓ Push 成功 → origin/{branch}")
                return
            wait = 2 ** attempt
            print(f"  Push 失敗（嘗試 {attempt+1}/4），{wait}s 後重試...")
            time.sleep(wait)

        print("  ✗ Push 全部失敗，請手動執行 git push")

    except subprocess.CalledProcessError as exc:
        print(f"  ✗ Git 操作失敗：{exc.stderr.decode().strip()}")


# ══════════════════════════════════════════════════════════════════════════════
# 主程式入口
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="本地端 BTC/USDT 15m K 線收集器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python collector/btc_price_collector.py                    # 更新所有年份
  python collector/btc_price_collector.py --year 2021        # 只更新 2021 年
  python collector/btc_price_collector.py --from-year 2020   # 從 2020 到現在
  python collector/btc_price_collector.py --push             # 完成後自動 git push
  python collector/btc_price_collector.py --from-year 2017 --push  # 組合使用
        """
    )
    parser.add_argument("--year",      type=int, help="只收集指定年份")
    parser.add_argument("--from-year", type=int, help="從指定年份收集到今年（預設 2013）",
                        dest="from_year")
    parser.add_argument("--push",      action="store_true", help="收集後自動 git commit & push")
    args = parser.parse_args()

    current_year = datetime.now(timezone.utc).year

    # 決定要收集哪些年份
    if args.year:
        years = [args.year]
    elif args.from_year:
        years = list(range(args.from_year, current_year + 1))
    else:
        # 預設：從 Kraken 最早年份到今年
        years = list(range(KRAKEN_START_YEAR, current_year + 1))

    print("=" * 55)
    print("  BTC/USDT 15m K 線收集器")
    print(f"  目標年份：{years[0]} ~ {years[-1]}")
    print(f"  存儲目錄：{DB_DIR}")
    print("=" * 55)

    total_new = 0
    for year in years:
        print(f"\n【{year} 年】")
        added = collect_year(year)
        total_new += added

    print("\n" + "=" * 55)
    print(f"  完成！本次新增 {total_new:,} 筆 15m K 線")
    print("=" * 55)

    # 市場快照（OI / 資金費率 / BTC.D）— 隨 collector 累積相對高點歷史
    print("\n【市場快照】OI / 資金費率 / BTC.D")
    snapshot_market_state()

    if args.push:
        git_push()
    else:
        print("\n提示：加上 --push 參數可自動提交並推送至雲端")
        print("      或手動執行：git add db/ && git commit -m 'data: update klines' && git push")


if __name__ == "__main__":
    main()
