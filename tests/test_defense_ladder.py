# -*- coding: utf-8 -*-
"""
tests/test_defense_ladder.py — 1 BTC ROAD 防守推移表守門（2026-07-04 C-1 修正案）

守三件事：
  1. 表內強平價與逆合約公式自洽（1/Liq' = 1/Liq + ΔM/Position_USD；
     常數正本 = vault「1b 馬丁格爾數學稽核.md」實測反推值）
  2. 警報門檻 = 第 1 階觸發價（警報即行動訊號，C4 拍板）；階梯單調遞減
  3. 推播文案由 DEFENSE_LADDER 動態組裝：含全部觸發價/強平價/條件式提醒，
     且已觸發階標 🔴、未觸發標 ⚪（防止舊版「寫死過時計畫數字」問題復發）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from config import ALERT_PRICE_LOW, DEFENSE_LADDER
from service.notification.facade import build_defense_message

# S-1（2026-07-06）：驗算常數（現況強平價／Position_USD 反推值）改由私有來源載入，
# 公開 repo 讀不到 config_private 時本測試優雅 skip（不影響其他測試套件）。
# 正本：vault「1b 馬丁格爾數學稽核.md」的實測反推常數。
try:
    from config_private import LIQ_BASE as _LIQ_BASE, POSITION_USD as _POSITION_USD
except ImportError:
    _LIQ_BASE = _POSITION_USD = None


def test_ladder_liq_prices_match_formula():
    """逐階重算強平價，與表值誤差須 < 0.5%（表值來自 vault，捨入差可容忍）。"""
    if _LIQ_BASE is None:
        pytest.skip("config_private.py 缺失，跳過需要真實驗算常數的測試（S-1 覆蓋層）")
    inv_liq = 1.0 / _LIQ_BASE
    for i, (trig, action, add_btc, liq_after, note) in enumerate(DEFENSE_LADDER, 1):
        inv_liq += add_btc / _POSITION_USD
        computed = 1.0 / inv_liq
        err = abs(computed - liq_after) / liq_after
        assert err < 0.005, (
            f'第{i}階強平價不自洽：表值 {liq_after:,.0f} vs 公式 {computed:,.0f}（誤差 {err:.2%}）')


def test_alert_threshold_is_stage1_trigger():
    assert ALERT_PRICE_LOW == DEFENSE_LADDER[0][0], (
        f'警報門檻 {ALERT_PRICE_LOW} 應等於第 1 階觸發價 {DEFENSE_LADDER[0][0]}（C4 拍板）')


def test_ladder_monotonic():
    trigs = [s[0] for s in DEFENSE_LADDER]
    liqs  = [s[3] for s in DEFENSE_LADDER]
    assert trigs == sorted(trigs, reverse=True), '觸發價須嚴格遞減'
    assert liqs == sorted(liqs, reverse=True), '強平價須嚴格遞減'
    for trig, _, _, liq, _ in DEFENSE_LADDER:
        assert liq < trig, '每階強平價須低於其觸發價'


def test_message_contains_all_stages_and_conditions():
    msg = build_defense_message(53_000.0, now_str='TEST')
    for trig, action, add_btc, liq_after, _ in DEFENSE_LADDER:
        assert f'${trig:,.0f}' in msg, f'文案缺觸發價 {trig}'
        assert f'${liq_after:,.0f}' in msg, f'文案缺強平價 {liq_after}'
    assert 'final_low' in msg, '文案缺條件式提醒（執行前看模型熊底）'
    assert '重算' in msg, '文案缺「馬丁重啟即作廢」提醒'
    # 舊版錯誤數字不得復發（$37,000 單獨出現＝關兩台馬丁的錯置敘述）
    assert '關閉 2 台' not in msg and '$47,000' not in msg


def test_message_stage_marks_follow_price():
    # 測試價由 DEFENSE_LADDER 動態推導（不寫死絕對數字，S-1 覆蓋層後測試不再洩漏真實門檻）
    trig1, trig2, trig3 = (s[0] for s in DEFENSE_LADDER)
    mid_price = (trig1 + trig2) / 2   # 介於第 1、2 階之間：僅第 1 階已觸發
    msg = build_defense_message(mid_price, now_str='TEST')
    l1 = next(l for l in msg.splitlines() if '第1階' in l)
    l2 = next(l for l in msg.splitlines() if '第2階' in l)
    l3 = next(l for l in msg.splitlines() if '第3階' in l)
    assert l1.startswith('🔴') and l2.startswith('⚪') and l3.startswith('⚪')
    below_all = trig3 - 1   # 低於全部三階觸發價
    msg2 = build_defense_message(below_all, now_str='TEST')
    assert all(next(l for l in msg2.splitlines() if f'第{i}階' in l).startswith('🔴')
               for i in (1, 2, 3))


if __name__ == '__main__':
    test_ladder_liq_prices_match_formula()
    test_alert_threshold_is_stage1_trigger()
    test_ladder_monotonic()
    test_message_contains_all_stages_and_conditions()
    test_message_stage_marks_follow_price()
    print('防守推移表守門 5 項全部通過。')
    print('\n──── 推播文案 dry-run（現價 53,000）────')
    print(build_defense_message(53_000.0))
