#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/defense_config_check.py — 防守數字三處同步哨兵（2026-07-14 稽核 No.1 立）

背景：防守數字活在三處——本地 config_private.py／repo 內 .example schema／
GitHub Secret DEFENSE_CONFIG_JSON。secret 內容無法讀取，drift 無哨兵
＝警報可能推錯觸發價。本腳本走 config._load_defense_config() **同一條載入
路徑**算 canonical 指紋：本地跑＝config_private 指紋；Actions 跑＝secret 指紋。
兩邊指紋相同＝三處同步（.example 只驗 schema 形狀，不含真值）。

用法：
  本地：python scripts/defense_config_check.py            # 印 schema 驗證＋本地指紋
  Actions：python scripts/defense_config_check.py --push  # 指紋只推 LINE 不落 log
          （公開 repo 的 Actions log 任何人可見，指紋雖為單向雜湊仍不落地——
          防守數字取值空間有限，公開雜湊有離線窮舉風險）

維護流程（改 config_private.py 後必走）：
  1. 本地跑本腳本 → 記下指紋
  2. 更新 DEFENSE_CONFIG_JSON secret
  3. gh workflow run price_alert.yml -f verify_config=true → LINE 收 secret 側指紋
  4. 兩指紋一致才算同步完成
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def defense_fingerprint() -> str:
    """經 config 載入器取防守四元組，算 canonical JSON 的 sha256 前 16 碼。

    來源由 config.py 決定（DEFENSE_CONFIG_JSON 環境變數優先，否則 config_private），
    保證本地與 Actions 走同一條 parse 路徑。
    """
    import config
    return _fingerprint(config.ALERT_PRICE_LOW, config.DEFENSE_LADDER,
                        config.DEFENSE_DECISION_CARD, config.MART_TP_BASELINE)


def _fingerprint(alp, ladder, card, mart_baseline) -> str:
    """純函數供測試：四元組 → canonical 指紋（tuple/list 差異正規化為 list）。"""
    canon = json.dumps({
        'alert_price_low': float(alp),
        'defense_ladder': [list(r) for r in ladder],
        'defense_decision_card': list(card),
        'mart_tp_baseline': mart_baseline,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()[:16]


def validate_local_schema() -> list:
    """本地 config_private 對 .example 的 schema 形狀驗證（不比值）。回傳問題清單。"""
    from importlib.machinery import SourceFileLoader
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    problems = []
    try:
        cp = SourceFileLoader('_cp_chk', os.path.join(root, 'config_private.py')).load_module()
    except FileNotFoundError:
        return ['config_private.py 不存在（Actions 環境屬正常，本地環境須建立）']
    ex = SourceFileLoader('_ex_chk', os.path.join(root, 'config_private.py.example')).load_module()

    cp_attrs = {k for k in vars(cp) if k.isupper()}
    ex_attrs = {k for k in vars(ex) if k.isupper()}
    missing = ex_attrs - cp_attrs
    extra = cp_attrs - ex_attrs
    if missing:
        problems.append(f'config_private 缺 example 有的鍵：{sorted(missing)}')
    if extra:
        problems.append(f'config_private 多出 example 沒有的鍵（example schema 落後？）：{sorted(extra)}')
    if hasattr(cp, 'DEFENSE_LADDER'):
        widths = {len(r) for r in cp.DEFENSE_LADDER}
        if widths != {5}:
            problems.append(f'DEFENSE_LADDER 每列應 5 欄（trig/action/add_btc/liq_after/note），實際 {sorted(widths)}')
    if hasattr(cp, 'DEFENSE_DECISION_CARD') and len(cp.DEFENSE_DECISION_CARD) < 6:
        problems.append(f'DEFENSE_DECISION_CARD 應 ≥6 行（含 C-R5 反彈側＋U5-2 預設政策），實際 {len(cp.DEFENSE_DECISION_CARD)}')
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--push', action='store_true',
                    help='指紋改推 LINE（Actions 用；公開 log 不落指紋）')
    args = ap.parse_args()

    fp = defense_fingerprint()
    source = 'DEFENSE_CONFIG_JSON (secret)' if os.getenv('DEFENSE_CONFIG_JSON') else 'config_private.py (local)'

    if args.push:
        from service.notification.core import _send_line_message
        ok = _send_line_message([{'type': 'text', 'text':
            f'🔐 防守 config 同步驗證\n來源: {source}\n指紋: {fp}\n'
            f'（與本地 defense_config_check.py 輸出比對，一致＝三處同步）'}])
        print(f'指紋已推 LINE: {ok}（log 不落指紋）')
        sys.exit(0 if ok else 1)

    print(f'來源: {source}')
    problems = validate_local_schema()
    if problems:
        for p in problems:
            print(f'  ⚠ {p}')
    else:
        print('  schema 驗證: OK（attrs/階梯形狀/卡行數 與 example 一致）')
    print(f'指紋: {fp}')


if __name__ == '__main__':
    main()
