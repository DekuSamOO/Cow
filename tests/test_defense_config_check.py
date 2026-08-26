"""防守三處同步哨兵：指紋純函數與 schema 驗證測試（不實際發送、不讀 secret）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

from defense_config_check import _fingerprint, validate_local_schema


def test_fingerprint_source_agnostic():
    """tuple（config_private 形態）與 list（JSON 形態）輸入必須得到相同指紋。"""
    ladder_t = ((50000.0, '動作', 0.05, 40000.0, '註'),)
    ladder_l = [[50000.0, '動作', 0.05, 40000.0, '註']]
    card_t = ('a', 'b')
    card_l = ['a', 'b']
    mtb = {'date': '2026-01-01', 'marts': [{'name': '馬1', 'tp': 99999.0, 'rung': 1}]}
    assert (_fingerprint(50000, ladder_t, card_t, mtb, 60000.0)
            == _fingerprint(50000.0, ladder_l, card_l, mtb, 60000.0))
    # None 也要來源無關（多數時候沒有活躍 D3 網格）
    assert (_fingerprint(50000, ladder_t, card_t, mtb, None)
            == _fingerprint(50000.0, ladder_l, card_l, mtb, None))


def test_fingerprint_sensitive_to_each_field():
    """任何一個欄位變動都必須改變指紋（drift 必被抓到）。"""
    base = dict(alp=50000.0, ladder=[[50000.0, 'x', 0.05, 40000.0, 'n']], card=['a'], mtb=None, d3=None)
    fp0 = _fingerprint(base['alp'], base['ladder'], base['card'], base['mtb'], base['d3'])
    assert _fingerprint(50001.0, base['ladder'], base['card'], base['mtb'], base['d3']) != fp0
    assert _fingerprint(base['alp'], [[50000.0, 'x', 0.06, 40000.0, 'n']], base['card'], base['mtb'], base['d3']) != fp0
    assert _fingerprint(base['alp'], base['ladder'], ['a', 'b'], base['mtb'], base['d3']) != fp0
    assert _fingerprint(base['alp'], base['ladder'], base['card'],
                        {'date': '2026-01-01', 'marts': []}, base['d3']) != fp0
    # D3 網格緩衝的觸發基準本身也要能被 drift 偵測抓到（本次新增，2026-08-26）
    assert _fingerprint(base['alp'], base['ladder'], base['card'], base['mtb'], 60000.0) != fp0


def test_local_schema_validation_passes():
    """現役 config_private 對 example 的 schema 驗證必須乾淨（缺檔時回報而非炸）。"""
    problems = validate_local_schema()
    allowed = [p for p in problems if 'config_private.py 不存在' in p]   # CI 環境無私有檔屬正常
    assert problems == allowed, f'schema 驗證發現問題: {problems}'
