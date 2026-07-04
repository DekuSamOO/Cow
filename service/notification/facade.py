from datetime import datetime
from config import ALERT_PRICE_LOW, DEFENSE_LADDER
from service.notification.core import (
    _send_line_message,
    _send_telegram_message,
    _is_line_configured,
    _is_telegram_configured
)

def notify_swing_signal(
    signal_type: str,
    price: float,
    ema20: float,
    dist_pct: float,
    stop_price: float,
    capital: float = 0.0,
    use_line: bool = True,
    use_telegram: bool = True,
) -> dict:
    """
    波段策略訊號推播（同時支援 LINE + Telegram）。
    """
    result = {'line': False, 'telegram': False}

    signal_map = {
        'BUY':  ("🟢", "買進訊號 (BUY)", "甜蜜點！趨勢向上且回踩均線"),
        'SELL': ("🔴", "賣出訊號 (SELL)", "跌破均線，短期趨勢轉弱"),
        'WAIT': ("🟡", "乖離過大 (WAIT)", f"偏離 {dist_pct:.2f}%，勿追高"),
    }
    emoji, title, desc = signal_map.get(signal_type.upper(), ("🔵", signal_type, ""))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 組裝訊息內容（純文字版本，LINE & Telegram 共用）──────────────
    text_lines = [
        f"{emoji} 【Antigravity v4】{title}",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 時間: {now_str}",
        f"💰 BTC 現價: ${price:,.0f}",
        f"📐 EMA20: ${ema20:,.0f} (乖離 {dist_pct:+.2f}%)",
        f"🛑 建議止損: ${stop_price:,.0f}",
        "",
        f"📝 {desc}",
    ]
    if capital > 0:
        text_lines.append(f"💼 總資金: ${capital:,.0f}")
    plain_text = "\n".join(text_lines)

    # ── LINE 推播 ──────────────────────────────────────────────────────
    if use_line and _is_line_configured():
        result['line'] = _send_line_message([{"type": "text", "text": plain_text}])

    # ── Telegram 推播（使用 HTML 格式增強可讀性）──────────────────────
    if use_telegram and _is_telegram_configured():
        tg_lines = [
            f"{emoji} <b>【Antigravity v4】{title}</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"📅 時間: <code>{now_str}</code>",
            f"💰 BTC 現價: <b>${price:,.0f}</b>",
            f"📐 EMA20: ${ema20:,.0f} (乖離 <b>{dist_pct:+.2f}%</b>)",
            f"🛑 建議止損: <b>${stop_price:,.0f}</b>",
            "",
            f"📝 {desc}",
        ]
        if capital > 0:
            tg_lines.append(f"💼 總資金: <b>${capital:,.0f}</b>")
        result['telegram'] = _send_telegram_message("\n".join(tg_lines))

    return result

def notify_dual_invest_apy(
    product_type: str,
    strike: float,
    apy_pct: float,
    current_price: float,
    t_days: int,
    threshold_pct: float = 20.0,
    use_line: bool = True,
    use_telegram: bool = True,
) -> dict:
    """
    雙幣理財 APY 達標推播（同時支援 LINE + Telegram）。
    """
    result = {'line': False, 'telegram': False}

    if apy_pct < threshold_pct:
        return result

    product_map = {
        'SELL_HIGH': ("📈", "高賣 (持有BTC)", "Call Option"),
        'BUY_LOW':   ("📉", "低買 (持有USDT)", "Put Option"),
    }
    emoji, product_name, option_type = product_map.get(
        product_type.upper(), ("💰", product_type, "Unknown")
    )

    distance_pct = abs(strike / current_price - 1) * 100
    direction    = "高於" if product_type == 'SELL_HIGH' else "低於"
    now_str      = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 組裝訊息內容 ──────────────────────────────────────────────────
    text_lines = [
        f"{emoji} 【雙幣理財】APY 達標通知",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 時間: {now_str}",
        f"📦 產品: {product_name} ({option_type})",
        f"💰 BTC 現價: ${current_price:,.0f}",
        f"🎯 行權價: ${strike:,.0f}（{direction}現價 {distance_pct:.1f}%）",
        f"⏰ 期限: {t_days} 天",
        f"🔥 年化 APY: {apy_pct:.1f}% (門檻 {threshold_pct:.0f}%)",
        "",
        "⚠️ 注意：此為模型估算值，請結合市場情況判斷。",
    ]
    plain_text = "\n".join(text_lines)

    if use_line and _is_line_configured():
        result['line'] = _send_line_message([{"type": "text", "text": plain_text}])

    if use_telegram and _is_telegram_configured():
        tg_lines = [
            f"{emoji} <b>【雙幣理財】APY 達標通知</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"📅 時間: <code>{now_str}</code>",
            f"📦 產品: <b>{product_name}</b> ({option_type})",
            f"💰 BTC 現價: <b>${current_price:,.0f}</b>",
            f"🎯 行權價: <b>${strike:,.0f}</b>（{direction}現價 {distance_pct:.1f}%）",
            f"⏰ 期限: {t_days} 天",
            f"🔥 年化 APY: <b>{apy_pct:.1f}%</b>（門檻 {threshold_pct:.0f}%）",
            "",
            "⚠️ 注意：此為模型估算值，請結合市場情況判斷。",
        ]
        result['telegram'] = _send_telegram_message("\n".join(tg_lines))

    return result

def send_test_message(platform: str = "all") -> dict:
    """
    發送測試訊息，驗證推播設定是否正確。
    """
    result = {'line': False, 'telegram': False}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    test_text = (
        "✅ 比特幣投資戰情室 推播連線成功！\n"
        f"時間: {now_str}\n"
        "波段訊號與 APY 達標通知已啟用。"
    )

    if platform in ('line', 'all') and _is_line_configured():
        result['line'] = _send_line_message([{"type": "text", "text": test_text}])

    if platform in ('telegram', 'all') and _is_telegram_configured():
        tg_text = (
            "✅ <b>比特幣投資戰情室 Telegram Bot 連線成功！</b>\n"
            f"時間: <code>{now_str}</code>\n"
            "波段訊號與 APY 達標通知已啟用。"
        )
        result['telegram'] = _send_telegram_message(tg_text)

    return result


def build_defense_message(price: float, now_str: str = None) -> str:
    """
    防守推播文案 — 由 config.DEFENSE_LADDER 動態組裝（單一真實來源，數字勿在本檔寫死）。
    抽成純函數供測試對拍（2026-07-04 C-1 修正：舊版寫死「關 2 台馬丁→強平 $37,000」
    為過時計畫且數字錯置，詳見 _governance/AUDIT-SUMMARY C-1 與 vault 馬丁數學稽核）。
    依現價標示各階狀態：🔴=已觸發應執行、⚪=尚未觸發（完整推移表一次推送——
    price_alert 每日最多推一次，後續下探不會逐階再推）。
    """
    now_str = now_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"🛡️ BTC 跌破 ${ALERT_PRICE_LOW:,.0f}（1 BTC ROAD 防守事件）",
        "━━━━━━━━━━━━━━━━",
        f"💰 現價: ${price:,.0f}",
        "━━━━━━━━━━━━━━━━",
        "📋 防守推移表（🔴=已觸發）:",
    ]
    for i, (trig, action, add_btc, liq_after, note) in enumerate(DEFENSE_LADDER, 1):
        mark = "🔴" if price <= trig else "⚪"
        lines.append(f"{mark} 第{i}階 ${trig:,.0f}：{action}")
        lines.append(f"　　+{add_btc} BTC → 強平價 ~${liq_after:,.0f}")
        if note:
            lines.append(f"　　⚠ {note}")
    lines += [
        "━━━━━━━━━━━━━━━━",
        "🧭 執行前先看戰情室 final_low / ensemble_low（防守為條件式）",
        "🔁 馬丁若已止盈重啟，本表作廢——依 config.DEFENSE_LADDER 註解公式重算",
        f"🕐 時間: {now_str}",
    ]
    return "\n".join(lines)


def notify_defense_line(price: float) -> dict:
    """
    BTC 跌至防守線（config.ALERT_PRICE_LOW = 防守第 1 階觸發價）推播 —
    1 BTC ROAD 防守事件。文案見 build_defense_message（config.DEFENSE_LADDER 單一來源）。
    """
    result = {'line': False, 'telegram': False}
    text = build_defense_message(price)

    if _is_line_configured():
        result['line'] = _send_line_message([{"type": "text", "text": text}])
    if _is_telegram_configured():
        result['telegram'] = _send_telegram_message(text)

    return result


def notify_bear_bottom_score(
    score: int,
    signals_summary: str = "",
    threshold: int = 60,
) -> dict:
    """
    熊市底部評分達標推播。
    """
    result = {'line': False, 'telegram': False}
    if score < threshold:
        return result

    if score >= 75:
        level, action = "🔴 歷史極值底部 (All-In!)", "強烈建議大量積累"
    elif score >= 60:
        level, action = "🟠 明確底部區間", "建議重倉分批佈局"
    else:
        level, action = "🟡 可能底部區", "謹慎小倉試探"

    sig_line = f"\n📋 指標摘要: {signals_summary}" if signals_summary else ""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    text = (
        f"🐻 熊市底部獵人警報！\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏆 評分:  {score}/100\n"
        f"📊 狀態: {level}\n"
        f"💡 建議: {action}\n"
        f"⚙️  門檻: ≥ {threshold} 分{sig_line}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🕐 時間: {now_str}\n"
        f"⚠️ 此為自動推播，非投資建議"
    )
    
    if _is_line_configured():
        result['line'] = _send_line_message([{"type": "text", "text": text}])
    if _is_telegram_configured():
        result['telegram'] = _send_telegram_message(text)

    return result
