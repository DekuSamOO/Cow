# -*- coding: utf-8 -*-
"""
tests/c16_frozen_kline_backfill.py — C-16 一次性歷史凍結 K 線修復（2026-07-07）

背景：collector 增量起點舊版永久凍結「成形中」K 線（每日 01:00 UTC 排程時點各留一根
volume 遠低於鄰近根的壞棒）。程式端已修（fetch_start_ms 改回 last_ts，此後自癒），
本腳本針對「已經凍結」的歷史壞棒逐根重抓回補。

判定準則（沿用稽核方法）：hour==1（01:0x UTC，collector 排程時點）且
volume < 全年中位數 × 0.10。逐根重抓（前後各留一根 buffer 避免邊界漏抓），
INSERT OR REPLACE 覆蓋。

用法：python tests/c16_frozen_kline_backfill.py [--dry-run] [--years 2017,2022,2026]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import time

from collector.btc_price_collector import (
    DB_DIR, INTERVAL_MS, _binance_klines, get_db_path, insert_rows,
)


def find_frozen_rows(year: int):
    """回傳該年 DB 中判定為凍結壞棒的 (open_time, volume) 清單。"""
    db_path = get_db_path(year)
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT open_time, volume FROM klines ORDER BY open_time").fetchall()
    conn.close()
    if not rows:
        return []
    vols = sorted(v for _, v in rows)
    median_vol = vols[len(vols) // 2]
    threshold = median_vol * 0.10
    frozen = []
    for open_time, vol in rows:
        hour_utc = (open_time // 3_600_000) % 24
        if hour_utc == 1 and vol < threshold:
            frozen.append((open_time, vol))
    return frozen


def refetch_and_fix(year: int, frozen_rows, dry_run: bool):
    if not frozen_rows:
        print(f"  {year}：無凍結壞棒")
        return 0
    print(f"  {year}：發現 {len(frozen_rows)} 根凍結壞棒")
    if dry_run:
        for ot, v in frozen_rows[:10]:
            import datetime
            dt = datetime.datetime.fromtimestamp(ot / 1000, tz=datetime.timezone.utc)
            print(f"    {dt.strftime('%Y-%m-%d %H:%M UTC')}  volume={v}")
        if len(frozen_rows) > 10:
            print(f"    ...其餘 {len(frozen_rows) - 10} 根略")
        return len(frozen_rows)

    db_path = get_db_path(year)
    conn = sqlite3.connect(db_path)
    fixed = 0
    for open_time, old_vol in frozen_rows:
        start_ms = open_time
        end_ms = open_time + INTERVAL_MS * 2   # 含自身＋下一根，確保 API 回傳含此根
        try:
            fresh_rows = _binance_klines(start_ms, end_ms)
        except Exception as e:
            print(f"    [跳過] {open_time} 重抓失敗：{e}")
            continue
        match = [r for r in fresh_rows if r[0] == open_time]
        if not match:
            print(f"    [跳過] {open_time} 重抓結果無此根（可能已被交易所歸檔移除）")
            continue
        insert_rows(conn, match)
        new_vol = match[0][5]
        print(f"    修復 {open_time}：volume {old_vol} → {new_vol}")
        fixed += 1
        time.sleep(0.2)   # 避免撞 Binance rate limit
    conn.close()
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列出凍結壞棒，不重抓")
    ap.add_argument("--years", type=str, default="", help="逗號分隔年份，預設掃全部現有年檔")
    args = ap.parse_args()

    if args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        years = sorted(
            int(f.replace("btcusdt_15m_", "").replace(".db", ""))
            for f in os.listdir(DB_DIR)
            if f.startswith("btcusdt_15m_") and f.endswith(".db")
        )

    print(f"掃描年份：{years}")
    total_fixed = 0
    for year in years:
        frozen = find_frozen_rows(year)
        total_fixed += refetch_and_fix(year, frozen, args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] 共發現 {total_fixed} 根凍結壞棒（未修復，移除 --dry-run 才會實際重抓）")
    else:
        print(f"\n共修復 {total_fixed} 根凍結壞棒")


if __name__ == "__main__":
    main()
