# -*- coding: utf-8 -*-
"""
tests/test_defense_ladder.py — 1 BTC ROAD 防守推移表守門（2026-07-04 C-1 修正案）

守三件事：
  1. 表內強平價與逆合約公式自洽（1/Liq' = 1/Liq + ΔM/Position_USD；
     常數正本 = vault「1b 馬丁格爾數學稽核.md」實測反推值）
  2. 警報門檻 >= 第 1 階觸發價（2026-08-21 改判，見該測試 docstring）；階梯單調遞減
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
    _HAS_PRIVATE = True
except ImportError:
    _LIQ_BASE = _POSITION_USD = None
    _HAS_PRIVATE = False


def test_ladder_liq_prices_match_formula():
    """逐階重算強平價，與表值誤差須 < 0.5%（表值來自 vault，捨入差可容忍）。

    三種狀態分開處理（2026-08-24 修：舊版把後兩者混為一談，
    訊息寫「config_private.py 缺失」但其實檔案在，只是無部位）：
      (a) config_private 不存在（公開 repo／Actions）→ skip
      (b) 存在但 LIQ_BASE is None ＝ **無槓桿部位** → 改驗「無部位」的不變量
      (c) 存在且有部位 → 原本的逆合約公式自洽驗算
    """
    if not _HAS_PRIVATE:
        pytest.skip("config_private.py 不存在（公開 repo／Actions 環境屬正常），跳過驗算")
    if _LIQ_BASE is None:
        # 2026-08-24：網格 No.6 止盈平倉後無槓桿部位，強平價不存在。
        # 約定以 liq_after = 0.0 表示「無部位」；若有非 0 值，代表設定又描述了
        # 一個不存在的部位——那正是本次改版要根除的錯誤，必須擋下。
        assert _POSITION_USD is None, \
            'LIQ_BASE 為 None（無部位）時 POSITION_USD 也須為 None，否則兩者不一致'
        bad = [(i, row[3]) for i, row in enumerate(DEFENSE_LADDER, 1) if row[3] != 0.0]
        assert not bad, (
            f'無槓桿部位（LIQ_BASE=None）時階梯的 liq_after 必須全為 0.0，'
            f'但第 {[i for i, _ in bad]} 階仍有非 0 值——設定正在描述不存在的部位')
        return
    inv_liq = 1.0 / _LIQ_BASE
    for i, (trig, action, add_btc, liq_after, note) in enumerate(DEFENSE_LADDER, 1):
        inv_liq += add_btc / _POSITION_USD
        computed = 1.0 / inv_liq
        err = abs(computed - liq_after) / liq_after
        assert err < 0.005, (
            f'第{i}階強平價不自洽：表值 {liq_after:,.0f} vs 公式 {computed:,.0f}（誤差 {err:.2%}）')


def test_alert_threshold_leads_stage1_trigger():
    """警報門檻須不低於第 1 階觸發價（2026-08-21 判準修訂）。

    C4 舊判準是嚴格相等（「警報即行動訊號」）——當時第 1 階正好是關馬丁1，
    兩者天然同值。2026-08-21 兩台馬丁在高位連續止盈重啟，兩階最後加倉價上移後
    雙雙落到台股階觸發價**之下**，第 1 階因此換成台股；警報價經使用者複審維持
    原值不跟降，語義改為「高於全部三階、留足台股 T+2 的獨立預警價」
    （vault「1b 1 BTC ROAD」§4.2 結論 No.3）。

    （數字一律不寫進本檔——CLAUDE.md 陷阱 No.21：公開版控只寫顯假值。
    真值在 config_private.py／DEFENSE_CONFIG_JSON secret／vault §4.2。）

    故判準由 == 放寬為 >=；真正要守的不變量是「警報必須先於任何行動響起」。
    """
    assert ALERT_PRICE_LOW >= DEFENSE_LADDER[0][0], (
        f'警報門檻 {ALERT_PRICE_LOW} 不得低於第 1 階觸發價 {DEFENSE_LADDER[0][0]}'
        f'——低於即「該動手時才響」，台股 T+2 必然來不及')
    assert all(ALERT_PRICE_LOW >= trig for trig, *_ in DEFENSE_LADDER), (
        '警報須高於全部階梯觸發價，否則存在「行動已觸價但警報未響」的階')


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


# ── P4 馬丁止盈重啟偵測（2026-07-13）──────────────────────────
# 全部用範例假數字與假行情，不依賴 config_private / 網路。

_FAKE_BASELINE = {
    "date": "2026-01-01",
    "marts": [
        {"name": "馬1", "tp": 99_999.0, "rung": 1},
        {"name": "馬2", "tp": 88_888.0, "rung": 3},
    ],
}


def _fake_df(max_high):
    import pandas as pd
    return pd.DataFrame({"high": [max_high * 0.9, max_high], "close": [1.0, 1.0]})


def test_mart_restart_lines_variants():
    from service.notification.facade import _mart_restart_lines
    # 未執行/不可用 → 靜態警語（含「對帳重算」）
    static = _mart_restart_lines(None)
    assert len(static) == 1 and '對帳重算' in static[0]
    # 全部未達止盈 → 單行「仍有效」
    fresh = _mart_restart_lines(
        [{"name": "馬1", "tp": 99_999.0, "rung": 1, "max_high": 80_000.0, "restarted": False},
         {"name": "馬2", "tp": 88_888.0, "rung": 3, "max_high": 80_000.0, "restarted": False}],
        baseline_date="2026-01-01")
    assert len(fresh) == 1 and '仍有效' in fresh[0] and '2026-01-01' in fresh[0]
    # 馬2 已重啟 → 逐台警示、標明第 3 階作廢
    stale = _mart_restart_lines(
        [{"name": "馬1", "tp": 99_999.0, "rung": 1, "max_high": 90_000.0, "restarted": False},
         {"name": "馬2", "tp": 88_888.0, "rung": 3, "max_high": 90_000.0, "restarted": True}],
        baseline_date="2026-01-01")
    assert len(stale) == 1 and '馬2' in stale[0] and '第3階' in stale[0] and '作廢' in stale[0]


def test_detect_mart_restart_with_fake_market(monkeypatch):
    import service.market_data as md
    from service.notification import facade
    # 高點 90,000：馬2（tp 88,888）判重啟、馬1（tp 99,999）未重啟
    monkeypatch.setattr(md, 'fetch_binance_daily', lambda d: _fake_df(90_000.0))
    info = facade.detect_mart_restart(_FAKE_BASELINE)
    assert [m['restarted'] for m in info] == [False, True]
    assert info[1]['rung'] == 3 and info[0]['max_high'] == 90_000.0
    # Binance 空手 → Kraken 備援
    monkeypatch.setattr(md, 'fetch_binance_daily', lambda d: None)
    monkeypatch.setattr(md, 'fetch_kraken_daily', lambda d: _fake_df(100_000.0))
    info2 = facade.detect_mart_restart(_FAKE_BASELINE)
    assert [m['restarted'] for m in info2] == [True, True]
    # 兩備援皆失敗 → None（降級不阻斷）
    monkeypatch.setattr(md, 'fetch_kraken_daily', lambda d: (_ for _ in ()).throw(RuntimeError))
    assert facade.detect_mart_restart(_FAKE_BASELINE) is None
    # 基線未設定 → None
    assert facade.detect_mart_restart({}) is None


def test_message_embeds_restart_detection():
    stale_info = [
        {"name": "馬1", "tp": 99_999.0, "rung": 1, "max_high": 90_000.0, "restarted": False},
        {"name": "馬2", "tp": 88_888.0, "rung": 3, "max_high": 90_000.0, "restarted": True},
    ]
    msg = build_defense_message(53_000.0, now_str='TEST',
                                mart_restart=stale_info, baseline_date='2026-01-01')
    assert '第3階觸發價/釋出量作廢' in msg and '執行前必對帳重算' in msg
    # 未傳偵測結果 → 靜態警語不消失（既有守門的「重算」提醒仍在）
    msg2 = build_defense_message(53_000.0, now_str='TEST')
    assert '重算' in msg2


if __name__ == '__main__':
    test_ladder_liq_prices_match_formula()
    test_alert_threshold_leads_stage1_trigger()   # 2026-08-24 修：舊名已於 2026-08-21 改名，此處未同步
    test_ladder_monotonic()
    test_message_contains_all_stages_and_conditions()
    test_message_stage_marks_follow_price()
    print('防守推移表守門測試通過（P4 偵測測試需經 pytest 跑 monkeypatch 版）。')
    print('\n──── 推播文案 dry-run（現價 53,000）────')
    print(build_defense_message(53_000.0))
