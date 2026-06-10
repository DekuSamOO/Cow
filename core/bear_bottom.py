"""
core/bear_bottom.py
熊市底部獵人 — 指標計算與複合評分系統
純 Python，無 Streamlit 依賴
"""
import math
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime

from core.indicators import _ta_series


def calculate_bear_bottom_indicators(df):
    """
    計算 6 大熊市底部識別指標:
    1. Pi Cycle Bottom (SMA_111 vs 2×SMA_350)
    2. 200-Week SMA (SMA_1400)
    3. Puell Multiple Proxy (Price / SMA_365)
    4. Monthly RSI
    5. Power Law Support (Giovanni Santostasi 冪律模型)
    6. Mayer Multiple (Price / SMA_730)
    """
    df = df.copy()
    if df.empty:
        return df

    # 1. Pi Cycle Bottom（_ta_series：pandas-ta 在資料不足時回 None，統一轉 NaN Series）
    df['SMA_111'] = _ta_series(ta.sma(df['close'], length=111), df.index)
    df['SMA_350'] = _ta_series(ta.sma(df['close'], length=350), df.index)
    df['SMA_350x2'] = df['SMA_350'] * 2
    df['PiCycle_Gap'] = (df['SMA_111'] / df['SMA_350x2'] - 1) * 100

    # 2. 200-Week SMA (1400 days)
    df['SMA_1400'] = _ta_series(ta.sma(df['close'], length=1400), df.index)
    df['SMA200W_Ratio'] = df['close'] / df['SMA_1400'].where(df['SMA_1400'] > 0)

    # 3. Puell Multiple Proxy
    df['SMA_365'] = _ta_series(ta.sma(df['close'], length=365), df.index)
    df['Puell_Proxy'] = df['close'] / df['SMA_365'].where(df['SMA_365'] > 0)

    # 4. Monthly RSI
    monthly_close = df['close'].resample('MS').last()
    monthly_rsi = _ta_series(ta.rsi(monthly_close, length=14), monthly_close.index)
    df['RSI_Monthly'] = monthly_rsi.reindex(df.index).ffill()

    # 5. Power Law Support
    genesis_date = datetime(2009, 1, 3)
    days_arr = np.array([
        (d.to_pydatetime() - genesis_date).days
        if hasattr(d, 'to_pydatetime') else (d - genesis_date).days
        for d in df.index
    ], dtype=float)
    days_arr = np.clip(days_arr, 1, None)
    df['PowerLaw_Support'] = 10 ** (-17.01467 + 5.84 * np.log10(days_arr))
    df['PowerLaw_Ratio'] = df['close'] / df['PowerLaw_Support'].where(df['PowerLaw_Support'] > 0)

    # 6. Mayer Multiple (2年均線)
    df['SMA_730'] = _ta_series(ta.sma(df['close'], length=730), df.index)
    df['Mayer_Multiple'] = df['close'] / df['SMA_730'].where(df['SMA_730'] > 0)

    return df


def score_series(df):
    """
    向量化批量計算歷史評分序列 (取代逐行 iterrows)
    效能較 [calculate_bear_bottom_score(row) for row in df.iterrows()] 快 20-50x
    返回: pd.Series (index 同 df，值為 0-100 整數分)
    """
    s = pd.Series(0, index=df.index, dtype=float)

    # AHR999 (max 20)
    if 'AHR999' in df.columns:
        v = df['AHR999']
        s += np.where(v < 0.45, 20, np.where(v < 0.8, 13, np.where(v < 1.2, 5, 0)))

    # MVRV Z-Score (max 18)
    if 'MVRV_Z_Proxy' in df.columns:
        v = df['MVRV_Z_Proxy']
        s += np.where(v < -1.0, 18, np.where(v < 0, 12, np.where(v < 2.0, 4, 0)))

    # Pi Cycle Gap (max 15)
    if 'PiCycle_Gap' in df.columns:
        v = df['PiCycle_Gap']
        s += np.where(v < -10, 15, np.where(v < -3, 10, np.where(v < 5, 4, 0)))

    # 200W SMA Ratio (max 15)
    if 'SMA200W_Ratio' in df.columns:
        v = df['SMA200W_Ratio']
        s += np.where(v < 1.0, 15, np.where(v < 1.3, 11, np.where(v < 2.0, 5, np.where(v < 4.0, 1, 0))))

    # Puell Multiple (max 12)
    if 'Puell_Proxy' in df.columns:
        v = df['Puell_Proxy']
        s += np.where(v < 0.5, 12, np.where(v < 0.8, 8, np.where(v < 1.5, 3, 0)))

    # Monthly RSI (max 10)
    if 'RSI_Monthly' in df.columns:
        v = df['RSI_Monthly']
        s += np.where(v < 30, 10, np.where(v < 40, 7, np.where(v < 55, 2, 0)))

    # Power Law Ratio (max 5)
    if 'PowerLaw_Ratio' in df.columns:
        v = df['PowerLaw_Ratio']
        s += np.where(v < 2.0, 5, np.where(v < 5.0, 3, np.where(v < 10.0, 1, 0)))

    # Mayer Multiple (max 5)
    if 'Mayer_Multiple' in df.columns:
        v = df['Mayer_Multiple']
        s += np.where(v < 0.8, 5, np.where(v < 1.0, 3, np.where(v < 1.5, 1, 0)))

    return s.fillna(0).astype(int)


def calculate_bear_bottom_score(row):
    """
    單筆即時評分 (用於當前行顯示詳細 signals)
    批量歷史計算請改用 score_series(df) 以避免 N+1 效能問題

    返回: (score: int, signals: dict)

    [Fix] 無論指標值是否 NaN，均寫入 signals 字典（NaN 顯示為 '—'），
    確保 UI 卡片恆顯示全部 8 個指標格，不因數據不足而遺漏。
    """
    score = 0
    signals = {}

    def _is_nan(v):
        return v is None or (isinstance(v, float) and math.isnan(v))

    # 1. AHR999 囤幣指標 (最高 20 分)
    ahr = row.get('AHR999')
    if not _is_nan(ahr):
        if ahr < 0.45:
            s, label = 20, "🟢 歷史抄底區 (<0.45)"
        elif ahr < 0.8:
            s, label = 13, "🟡 偏低估 (0.45-0.8)"
        elif ahr < 1.2:
            s, label = 5, "⚪ 合理區間 (0.8-1.2)"
        else:
            s, label = 0, "🔴 高估 (>1.2)"
        score += s
        signals['AHR999'] = {'value': f"{ahr:.3f}", 'score': s, 'max': 20, 'label': label}
    else:
        # NaN: SMA200 歷史不足（< 200日），仍顯示指標卡
        signals['AHR999'] = {'value': '—', 'score': 0, 'max': 20, 'label': "⚪ 數據累積中 (需200日)"}

    # 2. MVRV Z-Score Proxy (最高 18 分)
    mvrv = row.get('MVRV_Z_Proxy')
    if not _is_nan(mvrv):
        if mvrv < -1.0:
            s, label = 18, "🟢 強力底部 (Z<-1)"
        elif mvrv < 0:
            s, label = 12, "🟡 低估 (-1~0)"
        elif mvrv < 2.0:
            s, label = 4, "⚪ 中性 (0~2)"
        else:
            s, label = 0, "🔴 高估/頂部 (>2)"
        score += s
        signals['MVRV_Z_Proxy'] = {'value': f"{mvrv:.2f}", 'score': s, 'max': 18, 'label': label}
    else:
        signals['MVRV_Z_Proxy'] = {'value': '—', 'score': 0, 'max': 18, 'label': "⚪ 數據累積中 (需200日)"}

    # 3. Pi Cycle Gap (最高 15 分)
    pi_gap = row.get('PiCycle_Gap')
    if not _is_nan(pi_gap):
        if pi_gap < -10:
            s, label = 15, "🟢 Pi週期深度底部區"
        elif pi_gap < -3:
            s, label = 10, "🟡 Pi週期底部接近"
        elif pi_gap < 5:
            s, label = 4, "⚪ Pi週期中性"
        else:
            s, label = 0, "🔴 遠離Pi週期底部"
        score += s
        signals['Pi_Cycle'] = {'value': f"{pi_gap:.1f}%", 'score': s, 'max': 15, 'label': label}
    else:
        signals['Pi_Cycle'] = {'value': '—', 'score': 0, 'max': 15, 'label': "⚪ 數據累積中 (需350日)"}

    # 4. 200-Week SMA Ratio (最高 15 分)
    sma200w = row.get('SMA200W_Ratio')
    if not _is_nan(sma200w):
        if sma200w < 1.0:
            s, label = 15, "🟢 跌破200週均 (歷史絕對底部)"
        elif sma200w < 1.3:
            s, label = 11, "🟡 接近200週均 (<1.3x)"
        elif sma200w < 2.0:
            s, label = 5, "⚪ 正常範圍 (1.3-2x)"
        elif sma200w < 4.0:
            s, label = 1, "🔴 偏高 (2-4x)"
        else:
            s, label = 0, "🔴🔴 極度高估 (>4x)"
        score += s
        signals['SMA_200W'] = {'value': f"{sma200w:.2f}x", 'score': s, 'max': 15, 'label': label}
    else:
        signals['SMA_200W'] = {'value': '—', 'score': 0, 'max': 15, 'label': "⚪ 數據累積中 (需1400日)"}

    # 5. Puell Multiple Proxy (最高 12 分)
    puell = row.get('Puell_Proxy')
    if not _is_nan(puell):
        if puell < 0.5:
            s, label = 12, "🟢 礦工恐慌/投降 (底部信號)"
        elif puell < 0.8:
            s, label = 8, "🟡 礦工承壓"
        elif puell < 1.5:
            s, label = 3, "⚪ 礦工正常獲利"
        else:
            s, label = 0, "🔴 礦工獲利豐厚/暴利"
        score += s
        signals['Puell_Multiple'] = {'value': f"{puell:.2f}", 'score': s, 'max': 12, 'label': label}
    else:
        signals['Puell_Multiple'] = {'value': '—', 'score': 0, 'max': 12, 'label': "⚪ 數據累積中 (需365日)"}

    # 6. Monthly RSI (最高 10 分)
    rsi_m = row.get('RSI_Monthly')
    if not _is_nan(rsi_m):
        if rsi_m < 30:
            s, label = 10, "🟢 月線嚴重超賣"
        elif rsi_m < 40:
            s, label = 7, "🟡 月線超賣"
        elif rsi_m < 55:
            s, label = 2, "⚪ 月線中性"
        else:
            s, label = 0, "🔴 月線強勢"
        score += s
        signals['RSI_Monthly'] = {'value': f"{rsi_m:.1f}", 'score': s, 'max': 10, 'label': label}
    else:
        signals['RSI_Monthly'] = {'value': '—', 'score': 0, 'max': 10, 'label': "⚪ 數據累積中 (需月頻RSI)"}

    # 7. Power Law Ratio (最高 5 分)
    pl_ratio = row.get('PowerLaw_Ratio')
    if not _is_nan(pl_ratio):
        if pl_ratio < 2.0:
            s, label = 5, "🟢 接近冪律支撐線"
        elif pl_ratio < 5.0:
            s, label = 3, "🟡 略高於冪律支撐"
        elif pl_ratio < 10.0:
            s, label = 1, "⚪ 正常範圍"
        else:
            s, label = 0, "🔴 遠高於冪律支撐"
        score += s
        signals['PowerLaw'] = {'value': f"{pl_ratio:.1f}x", 'score': s, 'max': 5, 'label': label}
    else:
        signals['PowerLaw'] = {'value': '—', 'score': 0, 'max': 5, 'label': "⚪ 數據累積中"}

    # 8. Mayer Multiple (最高 5 分)
    mayer = row.get('Mayer_Multiple')
    if not _is_nan(mayer):
        if mayer < 0.8:
            s, label = 5, "🟢 低於2年均線 (極度低估)"
        elif mayer < 1.0:
            s, label = 3, "🟡 低於2年均線"
        elif mayer < 1.5:
            s, label = 1, "⚪ 合理範圍"
        else:
            s, label = 0, "🔴 高於2年均線"
        score += s
        signals['Mayer_Multiple'] = {'value': f"{mayer:.2f}x", 'score': s, 'max': 5, 'label': label}
    else:
        signals['Mayer_Multiple'] = {'value': '—', 'score': 0, 'max': 5, 'label': "⚪ 數據累積中 (需730日)"}

    return score, signals


# ══════════════════════════════════════════════════════════════════════════════
# 市場多空評分 (-100 到 +100)
# ══════════════════════════════════════════════════════════════════════════════

def calculate_market_cycle_score_breakdown(row) -> tuple:
    """
    市場多空複合評分 + 各指標明細分解

    返回: (score, bear_total, bull_total, indicator_rows)
      score         : int，-100 到 +100
      bear_total    : int，熊底分數合計（0-100）
      bull_total    : int，牛頂分數合計（0-100）
      indicator_rows: list[dict]，每個指標的 {name, value, bear, bear_max, bull, bull_max}

    公式：score = bull_total - bear_total，clip 至 [-100, +100]
    分數若長時間不變屬正常現象，代表鏈上週期位置確實穩定維持在當前區間。
    """
    def _safe(v):
        return v if (v is not None and not (isinstance(v, float) and math.isnan(v))) else 0.0

    rows = []
    bear = 0
    bull = 0

    def _row(name, val_str, b, b_max, u, u_max):
        return {'name': name, 'value': val_str, 'bear': b, 'bear_max': b_max, 'bull': u, 'bull_max': u_max}

    # ── 1. AHR999 囤幣指標 ───────────────────────────────────────────────────
    ahr = _safe(row.get('AHR999'))
    b, u = 0, 0
    if ahr > 0:
        if   ahr < 0.45: b = 20
        elif ahr < 0.8:  b = 13
        elif ahr < 1.2:  b = 5
        if   ahr >= 2.0: u = 20
        elif ahr >= 1.5: u = 13
        elif ahr >= 1.2: u = 5
    bear += b; bull += u
    rows.append(_row('AHR999 囤幣指標', f'{ahr:.3f}' if ahr else '—', b, 20, u, 20))

    # ── 2. MVRV Z-Score 代理 ────────────────────────────────────────────────
    mvrv = _safe(row.get('MVRV_Z_Proxy'))
    b, u = 0, 0
    if   mvrv < -1.0: b = 18
    elif mvrv <  0:   b = 12
    elif mvrv <  2.0: b = 4
    if   mvrv >= 5.0: u = 18
    elif mvrv >= 3.5: u = 12
    elif mvrv >= 2.0: u = 4
    bear += b; bull += u
    rows.append(_row('MVRV Z-Score 代理', f'{mvrv:.2f}', b, 18, u, 18))

    # ── 3. Pi Cycle Gap ─────────────────────────────────────────────────────
    pi = _safe(row.get('PiCycle_Gap'))
    b, u = 0, 0
    if   pi < -10: b = 15
    elif pi < -3:  b = 10
    elif pi <  5:  b = 4
    if   pi >= 15: u = 15
    elif pi >= 10: u = 10
    elif pi >=  5: u = 4
    bear += b; bull += u
    rows.append(_row('Pi Cycle Gap (SMA111/SMA350×2-1)', f'{pi:.1f}%', b, 15, u, 15))

    # ── 4. 200 週 SMA 比率 ──────────────────────────────────────────────────
    sma = _safe(row.get('SMA200W_Ratio'))
    b, u = 0, 0
    if sma > 0:
        if   sma < 1.0: b = 15
        elif sma < 1.3: b = 11
        elif sma < 2.0: b = 5
        if   sma >= 5.0: u = 15
        elif sma >= 4.0: u = 11
        elif sma >= 3.0: u = 5
        elif sma >= 2.0: u = 1
    bear += b; bull += u
    rows.append(_row('200 週 SMA 比率 (現價/1400日均)', f'{sma:.2f}x' if sma else '—', b, 15, u, 15))

    # ── 5. Puell Multiple 代理 ──────────────────────────────────────────────
    puell = _safe(row.get('Puell_Proxy'))
    b, u = 0, 0
    if puell > 0:
        if   puell < 0.5: b = 12
        elif puell < 0.8: b = 8
        elif puell < 1.5: b = 3
        if   puell >= 4.0: u = 12
        elif puell >= 2.0: u = 8
        elif puell >= 1.5: u = 3
    bear += b; bull += u
    rows.append(_row('Puell Multiple 代理 (現價/365日均)', f'{puell:.2f}' if puell else '—', b, 12, u, 12))

    # ── 6. 月線 RSI ─────────────────────────────────────────────────────────
    rsi = _safe(row.get('RSI_Monthly'))
    b, u = 0, 0
    if rsi > 0:
        if   rsi < 30: b = 10
        elif rsi < 40: b = 7
        elif rsi < 55: b = 2
        if   rsi >= 75: u = 10
        elif rsi >= 65: u = 7
        elif rsi >= 55: u = 2
    bear += b; bull += u
    rows.append(_row('月線 RSI (14)', f'{rsi:.1f}' if rsi else '—', b, 10, u, 10))

    # ── 7. 冪律支撐比率 ─────────────────────────────────────────────────────
    pl = _safe(row.get('PowerLaw_Ratio'))
    b, u = 0, 0
    if pl > 0:
        if   pl < 2.0: b = 5
        elif pl < 5.0: b = 3
        if   pl >= 15: u = 5
        elif pl >= 10: u = 3
        elif pl >=  7: u = 1
    bear += b; bull += u
    rows.append(_row('冪律支撐比率 (現價/PowerLaw)', f'{pl:.1f}x' if pl else '—', b, 5, u, 5))

    # ── 8. Mayer 倍數（2 年均線）────────────────────────────────────────────
    mayer = _safe(row.get('Mayer_Multiple'))
    b, u = 0, 0
    if mayer > 0:
        if   mayer < 0.8: b = 5
        elif mayer < 1.0: b = 3
        if   mayer >= 2.4: u = 5
        elif mayer >= 2.0: u = 3
        elif mayer >= 1.5: u = 1
    bear += b; bull += u
    rows.append(_row('Mayer 倍數 (現價/730日均)', f'{mayer:.2f}x' if mayer else '—', b, 5, u, 5))

    score = max(-100, min(100, int(bull - bear)))
    return score, bear, bull, rows


def calculate_market_cycle_score(row) -> int:
    """
    市場多空複合評分 (-100 到 +100)
    公式：score = 牛頂分數 − 熊底分數，clip 至 [-100, +100]
    詳細指標分解請使用 calculate_market_cycle_score_breakdown()。
    """
    score, _, _, _ = calculate_market_cycle_score_breakdown(row)
    return score
