import json
import logging
from datetime import datetime
from config import ESCAPE_ALERT_TIERS
from core.season_forecast import STATS as _SEASON_STATS

logger = logging.getLogger(__name__)

# LINE Flex 單則硬上限 50KB（超限為 400 推播失敗）；超過軟上限即先砍新聞區塊自保。
_FLEX_SOFT_LIMIT_BYTES = 40 * 1024


def _payload_size_bytes(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

SEASON_BG_COLOR = {
    "spring": "#27AE60",
    "summer": "#E67E22",
    "autumn": "#E74C3C",
    "winter": "#2980B9",
}

SEASON_LIGHT_BG = {
    "spring": "#E8F5E9",
    "summer": "#FFF3E0",
    "autumn": "#FFEBEE",
    "winter": "#E3F2FD",
}

LIGHT_REMAP = {
    "#00ff88": "#27AE60",
    "#ff4b4b": "#E74C3C",
    "#ffeb3b": "#F39C12",
    "#aaaaaa": "#888888",
    "#ffffff": "#2C3E50",
    "#ffcc66": "#E67E22",
}

def _light(c: str) -> str:
    return LIGHT_REMAP.get(c, c)

def _build_season_box(s):
    """季節徽章（如「❄️ 冬季 — 深熊底部」）+ 週期進度 + 本輪峰值。無季節時回 None。"""
    if s["season_zh"] == "N/A":
        return None

    season_key = "winter"
    for k, color in SEASON_BG_COLOR.items():
        if color == s["season_color"]:
            season_key = k
            break

    contents = [
        {"type": "text", "text": f"{s['season_emoji']} {s['season_zh']}",
         "color": s["season_color"], "weight": "bold", "size": "md"},
    ]
    if s["season_desc"]:
        contents.append({"type": "text", "text": s["season_desc"],
                         "color": "#666666", "size": "xs", "margin": "xs", "wrap": True})

    meta_text = f"距上次減半 {s['days_since_halving']} 天（{s['halving_date_str']}）｜週期進度 {s['cycle_progress_pct']}%"
    contents.append({"type": "text", "text": meta_text,
                     "color": "#888888", "size": "xxs", "margin": "sm", "wrap": True})

    if s["cycle_ath"] > 0:
        peak_line = f"📈 本輪峰值 ${s['cycle_ath']:,.0f}（{s['cycle_ath_date']}）｜距峰 {s['from_high_pct']:+.1f}%"
        contents.append({"type": "text", "text": peak_line,
                         "color": "#E67E22", "size": "xxs", "margin": "xs", "wrap": True})

    return {
        "type": "box", "layout": "vertical",
        "margin": "md",
        "backgroundColor": SEASON_LIGHT_BG.get(season_key, "#F8F8F8"),
        "cornerRadius": "8px",
        "paddingAll": "md",
        "contents": contents,
    }


def _build_floor_support_box(s):
    items = [
        ("200週均線", s["floor_ma200w"]),
        ("冪律下界", s["floor_power_law"]),
        ("礦工電費", s["floor_miner_cost"]),
    ]
    rows = []
    current_price = s.get("current_price", 0)
    for label, floor in items:
        if floor is None or current_price <= 0:
            continue
        diff_pct = (current_price - floor) / floor * 100
        sign = "+" if diff_pct >= 0 else ""
        color = "#27AE60" if diff_pct >= 0 else "#E74C3C"
        rows.append({
            "type": "box", "layout": "horizontal", "margin": "xs",
            "contents": [
                {"type": "text", "text": label, "color": "#555555", "size": "xs", "flex": 3},
                {"type": "text", "text": f"${floor:,.0f}", "color": "#2C3E50", "size": "xs",
                 "weight": "bold", "align": "end", "flex": 3},
                {"type": "text", "text": f"{sign}{diff_pct:.1f}%", "color": color, "size": "xs",
                 "align": "end", "flex": 2},
            ],
        })

    if not rows:
        return None

    return {
        "type": "box", "layout": "vertical",
        "margin": "lg",
        "backgroundColor": "#F0F4FF",
        "cornerRadius": "8px",
        "paddingAll": "md",
        "contents": [
            {"type": "text", "text": "📊 底部支撐參考（日更）",
             "color": "#2C3E50", "size": "sm", "weight": "bold"},
            *rows,
            {"type": "text", "text": "綠=現價在支撐上方　紅=跌破支撐",
             "color": "#AAAAAA", "size": "xxs", "margin": "sm", "wrap": True},
        ],
    }

def _escape_color(score: int) -> str:
    if score >= 75: return "#C0392B"
    if score >= 60: return "#E67E22"
    if score >= 45: return "#F39C12"
    if score >= 25: return "#888888"
    return "#27AE60"


def _build_escape_box(s):
    """逃頂雷達（波段相對高點）— 每日 Flex 固定區塊。與 dashboard C5-A / BTC_WATCH 同源。
    無 escape_signals（compute 失敗）時回 None。"""
    sig = s.get("escape_signals")
    if not sig:
        return None
    score = s.get("escape_score", 0) or 0
    level = s.get("escape_level", "")
    color = _escape_color(score)
    left_flex = max(1, min(99, int(score)))

    names = {"derivatives": "① 合約過熱", "technical": "② 技術衰竭",
             "onchain": "③ 鏈上派發", "sentiment": "④ 情緒過熱", "macro": "⑤ 總經逆風"}
    rows = []
    for k, nm in names.items():
        v = sig.get(k, {})
        sc, mx = v.get("score", 0), v.get("max", 0)
        tag = "（未擬合）" if k == "onchain" else ""
        rows.append({
            "type": "box", "layout": "horizontal", "margin": "xs", "contents": [
                {"type": "text", "text": nm + tag, "color": "#555555", "size": "xs", "flex": 6},
                {"type": "text", "text": f"{sc}/{mx}", "color": color if sc > 0 else "#AAAAAA",
                 "size": "xs", "weight": "bold", "align": "end", "flex": 4},
            ],
        })

    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#FFF4F0", "cornerRadius": "8px", "paddingAll": "md",
        "contents": [
            {"type": "text", "text": "🚨 逃頂雷達（波段相對高點）", "weight": "bold",
             "color": "#2C3E50", "size": "sm"},
            {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                {"type": "box", "layout": "vertical", "flex": 7, "contents": [
                    {"type": "text", "text": level, "color": color, "weight": "bold", "size": "md", "wrap": True},
                ]},
                {"type": "box", "layout": "vertical", "flex": 3, "alignItems": "flex-end", "contents": [
                    {"type": "text", "text": f"{score}/100", "color": color, "size": "xl", "weight": "bold"},
                ]},
            ]},
            {"type": "box", "layout": "horizontal", "margin": "sm", "height": "6px", "contents": [
                {"type": "box", "layout": "vertical", "flex": left_flex, "backgroundColor": color, "contents": []},
                {"type": "box", "layout": "vertical", "flex": 100 - left_flex, "backgroundColor": "#E0E0E0", "contents": []},
            ]},
            *rows,
            {"type": "text", "text": "≥60 觸發逃頂警報｜風險量表非精準擇時，OI/BTC.D 歷史累積中",
             "color": "#AAAAAA", "size": "xxs", "margin": "sm", "wrap": True},
        ],
    }


def _low_color(score: int) -> str:
    """抄底分數色（高分=低估=綠；低分=無底部訊號=紅）。"""
    if score >= 60: return "#27AE60"
    if score >= 45: return "#F39C12"
    if score >= 25: return "#888888"
    return "#C0392B"


_ESCAPE_DIM_NAMES = {"derivatives": "合約過熱", "technical": "技術衰竭", "onchain": "鏈上派發",
                     "sentiment": "情緒過熱", "macro": "總經逆風"}
_LOW_DIM_NAMES = {"cycle": "長週期深跌", "derivatives": "合約超冷", "technical": "技術回穩",
                  "sentiment": "情緒恐慌", "onchain": "鏈上吸籌", "macro": "總經順風"}


def _dominant_dim(signals, names) -> str:
    """|score|/max 比例最高的維度顯示名（資料不足回 —）。
    逃頂/抄底各維分數皆 ≥0，取絕對值不影響；趨勢分數有號，取 |score| 找主導維度。"""
    best, best_r = "—", 0.0
    for k, nm in names.items():
        v = (signals or {}).get(k, {})
        mx = v.get("max", 0)
        if mx:
            r = abs(v.get("score", 0)) / mx
            if r > best_r:
                best_r, best = r, nm
    return best


_TREND_DIM_NAMES = {"ma_structure": "均線結構", "macd": "MACD動能",
                    "slope": "斜率動能", "adx": "ADX確信"}


def _build_trend_strip(s):
    """🧭 趨勢方向橫幅（波段雷達第三軸）：等級 + 有號分數 + 置中方向條（左空右多）。
    與 dashboard、BTC_WATCH 同源 core/trend_direction。無 trend_signals 時回 None。"""
    sig = s.get("trend_signals")
    if not sig:
        return None
    net = int(s.get("trend_score", 0) or 0)
    color = s.get("trend_color") or "#9E9E9E"
    mag = max(1, min(99, abs(net)))
    gray = {"type": "box", "layout": "vertical", "flex": 100, "backgroundColor": "#E0E0E0", "contents": []}
    fill = {"type": "box", "layout": "vertical", "flex": mag, "backgroundColor": color, "contents": []}
    rest = {"type": "box", "layout": "vertical", "flex": 100 - mag, "backgroundColor": "#E0E0E0", "contents": []}
    if net >= 0:
        half_l, half_r = [dict(gray)], [fill, rest]
    else:
        half_l, half_r = [rest, fill], [dict(gray)]
    return {
        "type": "box", "layout": "vertical", "margin": "sm", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "🧭 趨勢", "size": "xxs", "color": "#888888", "flex": 2},
                {"type": "text", "text": f"{s.get('trend_level', '')} {net:+d}",
                 "size": "xxs", "weight": "bold", "color": color, "flex": 6, "align": "end"},
            ]},
            {"type": "box", "layout": "horizontal", "margin": "sm", "height": "6px", "spacing": "xs", "contents": [
                {"type": "box", "layout": "horizontal", "flex": 1, "contents": half_l},
                {"type": "box", "layout": "horizontal", "flex": 1, "contents": half_r},
            ]},
            {"type": "text", "text": f"主導：{_dominant_dim(sig, _TREND_DIM_NAMES)}｜{s.get('trend_action', '')}",
             "color": "#888888", "size": "xxs", "margin": "sm", "wrap": True},
        ],
    }


def _swing_side(title, score, level, light_color, dom, delta=None):
    """波段雷達單側（逃頂或抄底）：標題 + 分數（含昨日 Δ）+ 等級 + 分數條 + 主導維度。"""
    bar = max(1, min(99, int(score)))
    score_text = f"{score}/100"
    if delta is not None:
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        score_text += f"（{arrow}{delta:+d}）"
    return {
        "type": "box", "layout": "vertical", "flex": 1, "contents": [
            {"type": "text", "text": title, "weight": "bold", "color": light_color, "size": "sm"},
            {"type": "text", "text": score_text, "color": light_color, "size": "xl", "weight": "bold"},
            {"type": "text", "text": level, "color": light_color, "size": "xxs", "wrap": True},
            {"type": "box", "layout": "horizontal", "margin": "sm", "height": "6px", "contents": [
                {"type": "box", "layout": "vertical", "flex": bar, "backgroundColor": light_color, "contents": []},
                {"type": "box", "layout": "vertical", "flex": 100 - bar, "backgroundColor": "#E0E0E0", "contents": []},
            ]},
            {"type": "text", "text": f"主導：{dom}", "color": "#888888", "size": "xxs", "margin": "sm", "wrap": True},
        ],
    }


def _build_swing_radar_box(s):
    """📍 波段雷達 — 逃頂／抄底左右並排雙計分。與 dashboard 波段雷達同源。無任一 signals 時回 None。"""
    esig = s.get("escape_signals")
    lsig = s.get("low_signals")
    if not esig and not lsig:
        return None
    e_score = s.get("escape_score", 0) or 0
    l_score = s.get("low_score", 0) or 0
    left = _swing_side("🚨 逃頂", e_score, s.get("escape_level", ""),
                       _escape_color(e_score), _dominant_dim(esig, _ESCAPE_DIM_NAMES),
                       delta=s.get("escape_delta"))
    right = _swing_side("🟢 抄底", l_score, s.get("low_level", ""),
                        _low_color(l_score), _dominant_dim(lsig, _LOW_DIM_NAMES),
                        delta=s.get("low_delta"))
    contents = [
        {"type": "text", "text": "📍 波段雷達", "weight": "bold", "color": "#2C3E50", "size": "sm"},
    ]
    trend_strip = _build_trend_strip(s)
    if trend_strip:
        contents.append(trend_strip)
        contents.append({"type": "separator", "margin": "sm"})
    contents.append(
        {"type": "box", "layout": "horizontal", "margin": "sm", "spacing": "md", "contents": [left, right]})
    # 三軸合成行動建議（與 dashboard 同源 core/action_ensemble；無資料時隱藏）
    if s.get("composite_action"):
        contents.append({
            "type": "text",
            "text": f"{s.get('composite_emoji', '🎯')} 今日行動：{s['composite_action']}"
                    f"｜{s.get('composite_pos', '')}",
            "color": _light(s.get("composite_color") or "#2C3E50"),
            "size": "xs", "weight": "bold", "margin": "sm", "wrap": True,
        })
    contents.append(
        {"type": "text", "text": "≥60 觸發逃頂／抄底警報 · 風險量表非精準擇時 · 倉位未擬合僅供參考",
         "color": "#AAAAAA", "size": "xxs", "margin": "sm", "wrap": True})
    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#F8F9FA", "cornerRadius": "8px", "paddingAll": "md",
        "contents": contents,
    }


def _build_risk_box(s):
    """🎯 風控停損參考（ATR 停損＋支撐壓力）— 同源 core/risk，與 watcher 一致。
    緊接在「今日行動」之後：行動建議講方向，這裡講萬一做了要怎麼設風控。
    每行一個概念（比照 `_build_floor_support_box` 的列表式排版，避免長句擠在窄欄位被截斷）。
    無 atr_risk 資料（ATR 欄缺/資料不足）時回 None。"""
    r = s.get("atr_risk")
    if not r:
        return None
    items = [
        ("停損（做多）", f"${r['stop_long']:,.0f}", None),
        ("停損（做空）", f"${r['stop_short']:,.0f}", None),
        ("支撐", f"${r['support']:,.0f}", f"{r['support_pct']:+.1f}%"),
        ("壓力", f"${r['resistance']:,.0f}", f"{r['resistance_pct']:+.1f}%"),
    ]
    rows = []
    for label, val, pct in items:
        contents = [
            {"type": "text", "text": label, "color": "#555555", "size": "xs", "flex": 4, "wrap": True},
            {"type": "text", "text": val, "color": "#2C3E50", "size": "xs", "weight": "bold",
             "align": "end", "flex": 3},
        ]
        if pct:
            contents.append({"type": "text", "text": pct, "color": "#2C3E50", "size": "xs",
                             "align": "end", "flex": 2})
        rows.append({"type": "box", "layout": "horizontal", "margin": "xs", "contents": contents})

    caption = f"ATR(14) ${r['atr']:,.0f}（近期日均波動 {r['atr_pct']:.1f}%）"
    if r.get("reward_risk") is not None:
        caption += f"｜上檔風報 1:{r['reward_risk']:.1f}"

    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#FFF9E6", "cornerRadius": "8px", "paddingAll": "md",
        "contents": [
            {"type": "text", "text": "🎯 風控停損參考", "color": "#2C3E50", "size": "sm", "weight": "bold"},
            *rows,
            {"type": "text", "text": caption, "color": "#888888", "size": "xxs",
             "margin": "sm", "wrap": True},
            {"type": "text", "text": "停損用 ATR 抓正常波動幅度，非精準點位；僅供參考",
             "color": "#AAAAAA", "size": "xxs", "margin": "xs", "wrap": True},
        ],
    }


def _build_advice_box(label, text, color):
    """黃底建議 box（每日 Flex「策略建議」與逃頂警報「操作建議」共用）。"""
    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#FFF9E6", "paddingAll": "md", "cornerRadius": "8px",
        "contents": [
            {"type": "text", "text": label, "color": "#888888", "size": "xxs", "weight": "bold"},
            {"type": "text", "text": text, "color": color,
             "size": "sm", "weight": "bold", "wrap": True, "margin": "xs"},
        ],
    }


# 逃頂警報分級樣式：等級名 → (header 標題, header 底色)。等級門檻見 config.ESCAPE_ALERT_TIERS。
_ESCAPE_TIER_STYLE = {
    "危急": ("🔥 BTC 逃頂危急", "#7B241C"),
    "警報": ("🚨 BTC 逃頂警報", "#C0392B"),
    "預警": ("⚠️ BTC 逃頂預警", "#E67E22"),
}


def escape_alert_tier(score):
    """逃頂警報分級：回傳 (tier_rank, tier_name)；未達最低門檻回 (0, None)。
    rank = 該級下限分數，數字越大越嚴重，供「升級才再推」比較用。"""
    for floor, name in ESCAPE_ALERT_TIERS:
        if score >= floor:
            return floor, name
    return 0, None


def build_escape_alert_flex(s):
    """≥ 門檻時的獨立逃頂警報，用與每日 Flex 同一個 _build_escape_box，header 依分級配色。
    回傳完整 flex message；無 escape_signals 時回 None。"""
    box = _build_escape_box(s)
    if box is None:
        return None
    date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    score = s.get("escape_score", 0) or 0
    _, tier_name = escape_alert_tier(score)
    title, header_color = _ESCAPE_TIER_STYLE.get(tier_name, _ESCAPE_TIER_STYLE["警報"])

    body_contents = [
        {"type": "text", "text": f"💰 BTC {s.get('price', '')}", "weight": "bold",
         "size": "lg", "color": "#E74C3C"},
    ]
    prev = s.get("escape_prev_score")
    if prev is not None:
        delta = score - prev
        body_contents.append({
            "type": "text", "text": f"較上次警報 {delta:+d} 分（{prev} → {score}）",
            "color": "#888888", "size": "xs", "margin": "sm",
        })
    body_contents.append(box)
    if s.get("escape_action"):
        body_contents.append(_build_advice_box("💡 操作建議", s["escape_action"], header_color))

    bubble = {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": header_color,
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": title, "weight": "bold",
                 "color": "#FFFFFF", "size": "xl"},
                {"type": "text", "text": f"更新時間: {date_str}", "color": "#FFFFFF",
                 "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFFFFF",
            "spacing": "sm", "contents": body_contents,
        },
    }
    return {"type": "flex", "altText": f"{title} {score}/100", "contents": bubble}


def build_action_alert_flex(data, prev_label=None):
    """三軸合成行動翻轉的獨立 LINE Flex bubble（header 用 composite_color 配色）。
    與 build_escape_alert_flex 同層，集中所有 Flex 於本模組（單一真實來源）。"""
    color = data.get("composite_color", "#555555")
    action = data.get("composite_action", "")
    pos = data.get("composite_pos", "")
    body = [
        {"type": "text", "text": f"{prev_label or '—'}  →  {action}",
         "weight": "bold", "size": "md", "wrap": True, "color": color},
        {"type": "text", "text": data.get("composite_detail", ""),
         "size": "sm", "color": "#555555", "wrap": True, "margin": "sm"},
    ]
    if pos:
        body.append({"type": "text", "text": pos, "size": "xs", "color": "#999999", "margin": "sm"})
    body.append({"type": "separator", "margin": "md"})
    body.append({"type": "text",
                 "text": f"趨勢 {data.get('trend_score')}｜逃頂 {data.get('escape_score')}｜抄底 {data.get('low_score')}",
                 "size": "xs", "color": "#888888", "wrap": True, "margin": "md"})
    bubble = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": color,
                   "contents": [{"type": "text", "text": f"{data.get('composite_emoji', '')} 操作訊號翻轉",
                                 "color": "#ffffff", "weight": "bold", "size": "lg"}]},
        "body": {"type": "box", "layout": "vertical", "spacing": "none", "contents": body},
    }
    return {"type": "flex", "altText": f"操作訊號翻轉：{prev_label or '—'} → {action}", "contents": bubble}


def _weekly_stat_box(title, hi, lo, cur, cur_color, hi_is_good):
    """週報單一分數區塊：週高/週低/現值三格橫排。
    hi_is_good=True（抄底，高=低估=機會）週高綠；False（逃頂，高=過熱=風險）週高紅。"""
    def cell(lbl, val, color):
        return {"type": "box", "layout": "vertical", "flex": 1, "contents": [
            {"type": "text", "text": lbl, "color": "#888888", "size": "xxs", "align": "center"},
            {"type": "text", "text": f"{val:.0f}", "color": color, "size": "lg",
             "weight": "bold", "align": "center"},
        ]}
    hi_color = "#27AE60" if hi_is_good else "#C0392B"
    return {
        "type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#F8F9FA",
        "cornerRadius": "8px", "paddingAll": "md", "contents": [
            {"type": "text", "text": title, "weight": "bold", "color": "#2C3E50", "size": "sm"},
            {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                cell("週高", hi, hi_color), cell("週低", lo, "#888888"),
                cell("現值", cur, cur_color),
            ]},
        ],
    }


def build_weekly_flex(data, esc, low, today):
    """
    週日傍晚週報 Flex（giga 單 bubble）。內容＝本週價格區間 + 逃頂/抄底分週高低 + 趨勢 + 行動。
    esc / low：本週逃頂 / 抄底分數序列（list，最後一筆為現值）；可為空。
    資料完全不足（無價格且無任何分數）時回 None → 呼叫端退回文字版。
    """
    has_price = data.get("week_change_pct") is not None
    if not has_price and not esc and not low:
        return None

    header_color = "#2C3E50"
    body = []

    if has_price:
        chg = data["week_change_pct"]
        arrow = "📈" if chg >= 0 else "📉"
        chg_color = "#27AE60" if chg >= 0 else "#E74C3C"
        body.append({"type": "box", "layout": "vertical", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": f"{arrow} 本週 {chg:+.1f}%", "color": chg_color,
                 "size": "xl", "weight": "bold", "flex": 0},
                {"type": "text", "text": f"現價 {data.get('price', '—')}", "color": "#2C3E50",
                 "size": "sm", "align": "end", "gravity": "bottom"},
            ]},
            {"type": "text", "text": f"週高 ${data['week_high']:,.0f}　週低 ${data['week_low']:,.0f}",
             "color": "#888888", "size": "xs", "margin": "xs"},
        ]})

    if esc:
        body.append(_weekly_stat_box("🚨 逃頂分（週）", max(esc), min(esc), esc[-1],
                                     _escape_color(int(esc[-1])), hi_is_good=False))
    if low:
        body.append(_weekly_stat_box("🟢 抄底分（週）", max(low), min(low), low[-1],
                                     _low_color(int(low[-1])), hi_is_good=True))
    if data.get("trend_level"):
        body.append({"type": "text", "text": f"🧭 趨勢：{data['trend_level']}",
                     "color": "#2C3E50", "size": "sm", "margin": "md", "wrap": True})
    if data.get("composite_action"):
        body.append(_build_advice_box(
            "🎯 本週行動",
            f"{data['composite_action']}｜{data.get('composite_pos', '')}", header_color))

    bubble = {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": header_color,
            "paddingAll": "16px", "contents": [
                {"type": "text", "text": "📒 BTC 週報", "weight": "bold",
                 "color": "#FFFFFF", "size": "xl"},
                {"type": "text", "text": today, "color": "#FFFFFF", "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFFFFF",
            "spacing": "sm", "contents": body,
        },
    }
    msg = {"type": "flex", "altText": f"📒 BTC 週報 {today}", "contents": bubble}
    if _payload_size_bytes(msg) > _FLEX_SOFT_LIMIT_BYTES:
        logger.warning("[weekly_flex] payload 超軟上限（週報無新聞區塊可砍），仍送出")
    return msg


def _build_forecast_box(s):
    is_bear = s["forecast_type"] == "bear_bottom"
    title = "❄️ 熊市最低價預測" if is_bear else "🚀 牛市最高價預測"
    accent_color = "#2980B9" if is_bear else "#E67E22"
    bg = "#FFF8F0" if is_bear else "#F0FFF4"

    contents = [
        {"type": "text", "text": title, "color": accent_color, "size": "sm", "weight": "bold"},
        {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": s["label_low"], "color": "#666666", "size": "xxs", "align": "center"},
                {"type": "text", "text": f'${s["target_low"]:,.0f}', "color": "#2C3E50", "size": "xs", "align": "center"},
            ]},
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "中位數", "color": accent_color, "size": "xxs", "align": "center"},
                {"type": "text", "text": f'${s["target_median"]:,.0f}', "color": accent_color, "size": "md", "weight": "bold", "align": "center"},
            ]},
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": s["label_high"], "color": "#666666", "size": "xxs", "align": "center"},
                {"type": "text", "text": f'${s["target_high"]:,.0f}', "color": "#2C3E50", "size": "xs", "align": "center"},
            ]},
        ]},
    ]

    if is_bear:
        ath_ref_str = f"${s['forecast_ath_ref']:,.0f}" if s.get("forecast_ath_ref") else "本輪 ATH"
        method_text = f"算法：歷史三輪「熊底/ATH」比值（13.1% / 15.7% / 22.5%）取四分位數，乘以參考 ATH {ath_ref_str}"
    else:
        method_text = "算法：歷史三輪「ATH/減半價」倍數取四分位數（含週期遞減），乘以本輪減半收盤價"
    contents.append({"type": "text", "text": method_text,
                     "color": "#888888", "size": "xxs", "margin": "sm", "wrap": True})

    if s.get("forecast_estimated_date") and s["forecast_estimated_date"] != "N/A":
        when_label = "預估底部時間" if is_bear else "預估高點時間"
        days_med = _SEASON_STATS["bottom_days_median"] if is_bear else _SEASON_STATS["peak_days_median"]
        contents.append({"type": "text",
                         "text": f"⏳ {when_label}：約 {s['forecast_estimated_date']}（歷史中位數減半後 {days_med} 天）",
                         "color": "#888888", "size": "xxs", "margin": "xs", "wrap": True})

    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": bg,
        "cornerRadius": "8px",
        "paddingAll": "md",
        "contents": contents,
    }

def _build_score_box(s, left_flex):
    return {
        "type": "box", "layout": "vertical", "margin": "md",
        "backgroundColor": "#F8F9FA", "cornerRadius": "8px", "paddingAll": "md",
        "contents": [
            {"type": "text", "text": "🧭 長週期多空評分", "weight": "bold",
             "color": "#2C3E50", "size": "sm"},
            {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                {"type": "box", "layout": "vertical", "flex": 7, "contents": [
                    {"type": "text", "text": s["cycle_name"], "color": s["cycle_color"],
                     "weight": "bold", "size": "md"},
                    {"type": "text", "text": s["cycle_advice"], "color": "#666666",
                     "size": "xs", "wrap": True},
                ]},
                {"type": "box", "layout": "vertical", "flex": 3, "alignItems": "flex-end", "contents": [
                    {"type": "text", "text": f"{s['cycle_score']:+d}", "color": s["cycle_color"],
                     "size": "xxl", "weight": "bold"},
                ]},
            ]},
            {"type": "box", "layout": "horizontal", "margin": "md", "height": "8px", "contents": [
                {"type": "box", "layout": "vertical", "flex": left_flex,
                 "backgroundColor": s["cycle_color"], "contents": []},
                {"type": "box", "layout": "vertical", "flex": 100 - left_flex,
                 "backgroundColor": "#E0E0E0", "contents": []},
            ]},
        ],
    }

def _build_news_box(s):
    """加密新聞輿情區塊：整體情緒燈號 + 數則重大新聞中文標題。
    無新聞資料（無金鑰/抓取失敗）時回 None，整個區塊省略，不影響其餘推播。
    """
    mood = s.get("news_mood")
    items = s.get("news_items") or []
    if not mood and not items:
        return None

    contents = [
        {"type": "text", "text": "📰 加密新聞輿情", "weight": "bold",
         "color": "#2C3E50", "size": "sm"},
    ]
    if mood:
        contents.append({"type": "text", "text": mood, "color": "#555555",
                         "size": "xs", "margin": "xs", "wrap": True})
    for it in items:
        contents.append({"type": "text",
                         "text": f"{it.get('emoji', '•')} {it.get('title', '')}",
                         "color": "#666666", "size": "xxs", "margin": "xs", "wrap": True})

    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#F5F0FF", "cornerRadius": "8px", "paddingAll": "md",
        "contents": contents,
    }


_KIND_COLOR = {
    "season":  "#E74C3C",   # 四季論趨勢底 / 模型（紅）
    "floor":   "#2980B9",   # 硬地板（藍）
    "anchor":  "#F1C40F",   # 錨點（黃）
    "warning": "#E67E22",   # 警示線（橙）
}


def _build_bottom_eval_box(s):
    """最低價綜合評估 — 單一 block，整合四季論趨勢底 + 4 floor + on-chain 錨 + 技術錨。
    資料源 core/bottom_floors（與 dashboard 同源）。無 bottom_eval 時回 None。"""
    be = s.get("bottom_eval")
    if not be or not be.get("estimates"):
        return None
    cp = be.get("current_price") or s.get("current_price", 0)

    header = [
        {"type": "text", "text": "📉 最低價綜合評估", "color": "#2C3E50", "size": "sm", "weight": "bold"},
    ]
    if be.get("final_low"):
        header.append({
            "type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                {"type": "text", "text": "最終最低價估計", "color": "#555555", "size": "xs", "flex": 5},
                {"type": "text", "text": f"${be['final_low']:,.0f}", "color": "#C0392B",
                 "size": "md", "weight": "bold", "align": "end", "flex": 5},
            ],
        })
        basis = be.get("final_low_basis") or ""
        ens = be.get("ensemble_low")
        sub = f"依據 {basis}" + (f"｜多錨中位數 ${ens:,.0f}" if ens else "")
        header.append({"type": "text", "text": sub, "color": "#888888", "size": "xxs", "margin": "xs", "wrap": True})

    # LINE 版精簡：只取代表性幾項 = 最高估計 + 3 硬地板 + 最低估計（去重後依價排序）
    # 完整 10 項在 dashboard D2.5 顯示。
    ests = be["estimates"]
    non_warn = [e for e in ests if e["kind"] != "warning"]
    floors   = [e for e in ests if e["kind"] == "floor"]            # 3 硬地板
    season_e = next((e for e in ests if e["kind"] == "season"), None)  # 四季論趨勢底
    pick = {}
    if non_warn:
        hi = max(non_warn, key=lambda x: x["value"])               # 最高估計
        lo = min(non_warn, key=lambda x: x["value"])               # 最低估計
        picks = [hi, *floors, lo]
        if season_e:
            picks.append(season_e)                                 # 永遠含四季論趨勢底
        for e in picks:
            pick[e["key"]] = e

    rows = []
    for e in sorted(pick.values(), key=lambda x: -x["value"]):
        v = e["value"]
        buf = (cp - v) / v * 100 if v else 0
        bcolor = "#27AE60" if buf >= 0 else "#E74C3C"
        rows.append({
            "type": "box", "layout": "horizontal", "margin": "xs", "contents": [
                {"type": "text", "text": e["label"], "color": _KIND_COLOR.get(e["kind"], "#555555"),
                 "size": "xxs", "flex": 5},
                {"type": "text", "text": f"${v:,.0f}", "color": "#2C3E50", "size": "xxs",
                 "weight": "bold", "align": "end", "flex": 4},
                {"type": "text", "text": f"{'+' if buf >= 0 else ''}{buf:.0f}%", "color": bcolor,
                 "size": "xxs", "align": "end", "flex": 3},
            ],
        })

    footer = {"type": "text",
              "text": "藍=硬地板　紅=四季論趨勢底　黃=鏈上/技術錨　右欄=現價距該價（完整 10 項見 App）",
              "color": "#AAAAAA", "size": "xxs", "margin": "sm", "wrap": True}

    return {
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#F0F4FF", "cornerRadius": "8px", "paddingAll": "md",
        "contents": header + rows + [footer],
    }


def _season_cell(label, value, dist, dist_color, accent):
    return {"type": "box", "layout": "vertical", "flex": 1, "contents": [
        {"type": "text", "text": label, "color": "#888888", "size": "xxs", "align": "center"},
        {"type": "text", "text": f"${value:,.0f}", "color": accent, "size": "md", "weight": "bold", "align": "center"},
        {"type": "text", "text": dist, "color": dist_color, "size": "xxs", "align": "center"},
    ]}


def _build_season_radar_box(s):
    """🗓️ 四季雷達 — 季節 + 週期頂底並排 + 通道條 + 頂錨依據 + 牛頂/熊底分 + 定位句。
    整合舊「季節徽章」，頂用 top_estimates 中位、底用 bottom_eval.final_low。無 season_zh 時回 None。"""
    if s.get("season_zh", "N/A") == "N/A":
        return None
    cp = s.get("current_price", 0) or 0
    tops = s.get("top_estimates") or []
    be = s.get("bottom_eval") or {}
    ct = s.get("cycle_top") or {}
    top_vals = sorted(t["value"] for t in tops)
    top_repr = top_vals[len(top_vals) // 2] if top_vals else None
    bottom_repr = be.get("final_low")

    contents = [
        {"type": "text", "text": "🗓️ 四季雷達", "weight": "bold", "color": "#2C3E50", "size": "sm"},
    ]

    cells = []
    if top_repr and cp:
        d = (top_repr / cp - 1) * 100
        cells.append(_season_cell("週期頂（中位錨）", top_repr, f"距頂 +{d:.0f}%", "#27AE60", "#E74C3C"))
    if bottom_repr and cp:
        d = (cp / bottom_repr - 1) * 100
        dc = "#27AE60" if d >= 0 else "#E74C3C"
        cells.append(_season_cell("四季論底", bottom_repr, f"距底 {'+' if d>=0 else ''}{d:.0f}%", dc, "#2980B9"))
    if cells:
        contents.append({"type": "box", "layout": "horizontal", "margin": "md", "spacing": "md", "contents": cells})

    if top_repr and bottom_repr and cp and top_repr > bottom_repr:
        pos = max(1, min(99, int((cp - bottom_repr) / (top_repr - bottom_repr) * 100)))
        contents.append({"type": "text", "text": f"現價 ${cp:,.0f}（通道 {pos}% 位置）",
                         "color": "#888888", "size": "xxs", "margin": "md"})
        contents.append({"type": "box", "layout": "horizontal", "margin": "xs", "height": "8px", "contents": [
            {"type": "box", "layout": "vertical", "flex": pos, "backgroundColor": "#2980B9", "contents": []},
            {"type": "box", "layout": "vertical", "flex": 100 - pos, "backgroundColor": "#E74C3C", "contents": []},
        ]})
        contents.append({"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": "底", "color": "#2980B9", "size": "xxs"},
            {"type": "text", "text": "頂", "color": "#E74C3C", "size": "xxs", "align": "end"},
        ]})

    # 頂錨依據明細（頂部價格依據列出）
    if tops:
        anchor_txt = "｜".join(f"{t['label']} ${t['value']:,.0f}" for t in tops[:3])
        contents.append({"type": "text", "text": f"頂依據：{anchor_txt}",
                         "color": "#999999", "size": "xxs", "margin": "md", "wrap": True})

    bull = ct.get("bull_total", 0)
    bear = ct.get("bear_total", 0)
    eff = ct.get("effective_season")
    contents.append({"type": "text", "text": f"牛頂分 {bull}/100 ┃ 熊底分 {bear}/100",
                     "color": "#555555", "size": "xs", "margin": "md", "weight": "bold"})
    if eff == "autumn":
        pos_txt = "🍂 高點已過、底部未至 → 逐步減倉"
    elif bull >= 60:
        pos_txt = "🔥 接近整輪大頂 → 分批止盈"
    elif eff == "winter" or bear >= 50:
        pos_txt = "❄️ 築底階段 → 定期定額囤幣"
    elif eff == "spring":
        pos_txt = "🌱 復甦初期 → 分批建倉"
    else:
        pos_txt = "☀️ 主升/中段 → 持有設移動止盈"
    contents.append({"type": "text", "text": pos_txt, "color": "#666666", "size": "xxs", "margin": "xs", "wrap": True})

    return {
        "type": "box", "layout": "vertical", "margin": "md",
        "backgroundColor": "#FFF8F0", "cornerRadius": "8px", "paddingAll": "md",
        "contents": contents,
    }


def build_flex_message(s):
    date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    left_flex = max(1, min(99, int((s["cycle_score"] + 100) / 2)))

    body_contents = [
        {"type": "text", "text": f"💰 BTC {s['price']}", "weight": "bold",
         "size": "xxl", "color": "#27AE60"},
    ]

    # 0. 健康檢查：本機 OI 快照排程靜默失敗 / ETF 快取過舊，從卡片直接看到
    stale_days = s.get("snapshot_stale_days")
    if stale_days is not None and stale_days > 2:
        body_contents.append({
            "type": "text", "text": f"⚠️ OI 快照已 {stale_days} 天未更新（檢查本機排程）",
            "color": "#E74C3C", "size": "xs", "wrap": True, "margin": "sm",
        })
    etf_stale = s.get("etf_stale_days")
    if etf_stale is not None and etf_stale > 4:
        body_contents.append({
            "type": "text", "text": f"⚠️ ETF 流量資料為 {etf_stale} 天前（本機跑 collector 可刷新）",
            "color": "#E67E22", "size": "xs", "wrap": True, "margin": "sm",
        })

    # 1. 季節徽章（冬季 — 深熊底部）
    season_box = _build_season_box(s)
    if season_box:
        body_contents.append(season_box)

    # 2. 長週期多空評分
    body_contents.append(_build_score_box(s, left_flex))

    # 3. 波段雷達（逃頂 + 抄底並排）
    swing_box = _build_swing_radar_box(s)
    if swing_box:
        body_contents.append(swing_box)

    # 3b. 風控框架（ATR 停損 + 支撐壓力風報比）— 緊接在今日行動之後
    risk_box = _build_risk_box(s)
    if risk_box:
        body_contents.append(risk_box)

    # 4. 四季雷達（季節 + 週期頂底 + 通道 + 頂錨依據 + 牛頂/熊底分）
    season_radar = _build_season_radar_box(s)
    if season_radar:
        body_contents.append(season_radar)

    # 5. 最低價綜合評估（底部完整明細，同源 core/bottom_floors）
    bottom_box = _build_bottom_eval_box(s)
    if bottom_box:
        body_contents.append(bottom_box)
    elif s.get("season_zh", "N/A") == "N/A":
        # 向後相容：無 bottom_eval（舊呼叫端）時退回原預測 + floor box
        body_contents.append(_build_forecast_box(s))
        floor_box = _build_floor_support_box(s)
        if floor_box:
            body_contents.append(floor_box)

    # 6. 加密新聞輿情
    news_box = _build_news_box(s)
    if news_box:
        body_contents.append(news_box)

    # 7. 策略建議
    body_contents.append(
        _build_advice_box("💡 策略建議", s["swing_advice"], _light(s["swing_advice_color"]))
    )

    flex_bubble = {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#E74C3C",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "🦅 戰情室決策速報", "weight": "bold",
                 "color": "#FFFFFF", "size": "xl"},
                {"type": "text", "text": f"更新時間: {date_str}", "color": "#FFFFFF",
                 "size": "xs", "margin": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFFFFF",
            "spacing": "sm",
            "contents": body_contents,
        },
    }
    msg = {"type": "flex", "altText": f"🦅 決策速報: BTC {s['price']}", "contents": flex_bubble}

    # 大小防線：超過軟上限先移除新聞區塊（資訊性最低、體積最大的區塊）
    size = _payload_size_bytes(msg)
    if size > _FLEX_SOFT_LIMIT_BYTES and news_box is not None:
        body_contents.remove(news_box)
        new_size = _payload_size_bytes(msg)
        logger.warning(f"[flex] payload {size}B 超過軟上限 {_FLEX_SOFT_LIMIT_BYTES}B，"
                       f"已移除新聞區塊 → {new_size}B")
        size = new_size
    if size > _FLEX_SOFT_LIMIT_BYTES:
        logger.warning(f"[flex] payload {size}B 仍超過軟上限（LINE 硬上限 50KB），推播可能失敗")
    else:
        logger.info(f"[flex] payload size = {size} bytes")
    return msg
