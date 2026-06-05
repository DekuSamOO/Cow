from datetime import datetime
from core.season_forecast import STATS as _SEASON_STATS

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

SEASON_DESC = {
    "spring": "減半後 0–12 個月，市場低調吸籌",
    "summer": "減半後 12–18 個月，主升浪爆發",
    "autumn": "減半後 18–36 個月，獲利了結回落",
    "winter": "減半後 36–48 個月，長期底部整理",
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

def _radar_row(label, value_text, value_color):
    return {
        "type": "box", "layout": "horizontal", "margin": "xs",
        "contents": [
            {"type": "text", "text": label, "color": "#666666", "size": "sm"},
            {"type": "text", "text": value_text, "color": _light(value_color), "size": "sm",
             "weight": "bold", "align": "end"},
        ],
    }

def _build_radar_box(s):
    return {
        "type": "box", "layout": "vertical",
        "margin": "lg",
        "backgroundColor": "#F8F9FA",
        "cornerRadius": "8px",
        "paddingAll": "md",
        "contents": [
            {"type": "text", "text": "🐂 波段雷達", "weight": "bold",
             "color": "#2C3E50", "size": "sm"},
            _radar_row("MA200 支撐",  s["ma200_label"],   "#2C3E50"),
            _radar_row("資金費率",    s["funding_text"],  s["funding_color"]),
            _radar_row("趨勢方向",    s["trend_text"],    s["trend_color"]),
            _radar_row("RSI 強弱",    s["rsi_text"],      s["rsi_color"]),
            _radar_row("MACD 交叉",   s["macd_text"],     s["macd_color"]),
            _radar_row("ADX 動能",    s["adx_text"],      s["adx_color"]),
            _radar_row("EMA20 乖離",  s["ema_dist_text"], s["ema_dist_color"]),
        ],
    }

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


def build_flex_message(s):
    date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    left_flex = max(1, min(99, int((s["cycle_score"] + 100) / 2)))

    body_contents = [
        {"type": "text", "text": f"💰 BTC {s['price']}", "weight": "bold",
         "size": "xxl", "color": "#27AE60"},
    ]

    season_box = _build_season_box(s)
    if season_box:
        body_contents.append(season_box)

    body_contents.append(_build_score_box(s, left_flex))
    body_contents.append(_build_radar_box(s))

    # 最低價綜合評估（單一 block，整合四季論趨勢底 + 4 floor + on-chain/技術錨）
    bottom_box = _build_bottom_eval_box(s)
    if bottom_box:
        body_contents.append(bottom_box)
    else:
        # 向後相容：無 bottom_eval（舊呼叫端）時退回原兩個分離 box
        body_contents.append(_build_forecast_box(s))
        floor_box = _build_floor_support_box(s)
        if floor_box:
            body_contents.append(floor_box)

    news_box = _build_news_box(s)
    if news_box:
        body_contents.append(news_box)

    body_contents.append({
        "type": "box", "layout": "vertical", "margin": "lg",
        "backgroundColor": "#FFF9E6", "paddingAll": "md", "cornerRadius": "8px",
        "contents": [
            {"type": "text", "text": "💡 策略建議", "color": "#888888", "size": "xxs", "weight": "bold"},
            {"type": "text", "text": s["swing_advice"], "color": _light(s["swing_advice_color"]),
             "size": "sm", "weight": "bold", "wrap": True, "margin": "xs"},
        ],
    })

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
    return {"type": "flex", "altText": f"🦅 決策速報: BTC {s['price']}", "contents": flex_bubble}
