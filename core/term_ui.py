"""
core/term_ui.py — 終端機畫框 / 排版 / 面板通用層（W-7，2026-07-06 自 BTC_WATCH.py 抽出）

背景：`watcher.py`（UniversalMonitor）長年直接 `from BTC_WATCH import _title, _row, ...`
綁死 BTC_WATCH 內部私名 helper——BTC_WATCH 任何畫框整形都可能靜默破 watcher。本檔把
「畫框/排版/面板組裝/K 線側欄/可中斷等待」這層與標的無關的終端機 UI 邏輯獨立出來，
BTC_WATCH.py 與 watcher.py 都改吃這裡（單一真實來源）。

BTC_WATCH.py 保留 re-export shim（`from core.term_ui import (...)`），內部呼叫端與既有
測試（`from BTC_WATCH import _title, ...`）零改動；本次搬移為純位置移動，不改任何邏輯
（含 `_panel_trend` 對 `core.trend_direction.trend_meta` 的既有耦合，原樣保留，非本次範圍）。
"""
import math
import os
import re
import time
import unicodedata

from core.trend_direction import trend_meta

# ──────────────────────────────────────────────────────────────────────────
# 進度條 / 顯示寬度 / 框線基礎
# ──────────────────────────────────────────────────────────────────────────

# 評分等級→燈號（逃頂與底部共用底色概念）
def _bar(score, cap):
    """以可得天花板 cap 為分母畫 10 格進度條。"""
    cap = max(cap, 1)
    filled = int(round(min(score, cap) / cap * 10))
    return "█" * filled + "░" * (10 - filled)


# 覆寫用逃生門：某個字元的實際渲染寬度與下方通則不符時才往這裡加，並註明實測依據。
# （⚠ U+26A0 原本列在這裡；2026-08-26 起已由 0x2600 段的 EAW 通則正確判為 1，不需個案豁免。）
_NARROW_SYMBOLS = set()


def _dw(s):
    """字串顯示寬度：全形/emoji=2、半形=1（FE0F 修飾符=0）。

    ⚠️ **0x2600–0x27BF（雜項符號與 Dingbats）不是每個都寬**，這是本函式最容易錯的地方。
    原本整段一律算 2，實測有六個常用字元會被高估（EAW=N、無 emoji presentation，
    終端機只佔一欄）：⚠ ❄ ✕ ☀ ⚙ ✓；真正寬的是 EAW=W 那些：⚪ ✅ ❌ ⛔ ❓ ⚡。
    後果是**每出現一個就少補一格空白、右框往左縮一欄**——2026-08-26 使用者回報
    「LINE 哨兵」那行錯位，該行有 4 個 ✕、右框整整少 4 欄。
    改為在這段內信任 `east_asian_width`（W/F 才算 2）。
    0x1F300–0x1FAFF 那段是真 emoji，維持一律 2。
    """
    w = 0
    for ch in s:
        if ch == "️":
            continue
        o = ord(ch)
        if o in _NARROW_SYMBOLS:
            w += 1
        elif 0x1F300 <= o <= 0x1FAFF:
            w += 2
        elif 0x2600 <= o <= 0x27BF:
            w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _row(content, W, v="│"):
    """左右框 + 內容補空格對齊到顯示寬度 W。v 為側框字元（雙線表頭用 ║）。"""
    return v + content + " " * max(0, W - _dw(content)) + v


def _edge(left, fill, right, W):
    return left + fill * W + right


def _title(text, W):
    """┌─ text ───┐ 形式，依顯示寬度補滿右框。"""
    head = f"─ {text} "
    return "┌" + head + "─" * max(0, W - _dw(head)) + "┐"


def _panel(result, meta_fn, cap, name, dims):
    """逃頂/抄底評分共用：把 (score, signals) 攤成 (title, rows)。result 為 None 時回 ("", [])。"""
    if result is None:
        return "", []
    score, sig = result
    level, _, action = meta_fn(score)
    title = f"{name}  {score}/100  可得≤{cap}  {_bar(score, cap)}  {level}"
    rows = [f"  {sig[d]['score']:>2}/{sig[d]['max']:<2}  {sig[d]['label']}" for d in dims]
    rows.append(f"  → {action}")
    return title, rows


def _bar_signed(net):
    """有號淨方向分（-100~+100）置中條：│ 左為空頭、右為多頭，各 5 格。"""
    mag = int(round(min(abs(net), 100) / 100 * 5))
    if net >= 0:
        return "░" * 5 + "│" + "█" * mag + "░" * (5 - mag)
    return "░" * (5 - mag) + "█" * mag + "│" + "░" * 5


def _short_momentum(df):
    """
    短線動能（補趨勢方向中長期軸缺的「這週」尺度）：近 7 日報酬 + 價 vs EMA_20 + RSI_14。
    純取已算好的日線欄位（每小時刷新一次的 df），不發網路請求；資料不足回 None。
    與趨勢方向軸正交：可「中期空頭 + 短線偏多」（短線反彈），正是區分「全面下跌 vs 反彈」之用。
    """
    if df is None or len(df) < 8:
        return None
    close = float(df["close"].iloc[-1])
    prev7 = float(df["close"].iloc[-8])
    ret7 = (close / prev7 - 1) * 100 if prev7 else 0.0

    def _last(col):
        if col not in getattr(df, "columns", []):
            return None
        v = df[col].iloc[-1]
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    ema20 = _last("EMA_20")
    rsi = _last("RSI_14")
    above = None if ema20 is None else close > ema20

    # 近 7 日方向與價對 EMA20 一致才表態（above is False 僅在 ema20 存在時成立）
    if ret7 > 0 and above is True:
        lbl = "🟢 短線偏多"
    elif ret7 < 0 and above is False:
        lbl = "🔴 短線偏空"
    else:
        lbl = "⚪ 短線中性"

    parts = [f"近7日 {ret7:+.1f}%"]
    if above is not None:
        parts.append("價>EMA20" if above else "價<EMA20")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    return f"{lbl}  " + "｜".join(parts)


def _panel_trend(result, name, dims):
    """趨勢方向專用：分數有號（多+/空−），不適用 0-100 進度條與「可得≤」語意。"""
    if result is None:
        return "", []
    net, sig = result
    level, _, action = trend_meta(net)
    title = f"{name}  {net:+d}/±100  {_bar_signed(net)}  {level}"
    rows = [f"  {sig[d]['score']:+3d}/±{sig[d]['max']:<2} {sig[d]['label']}" for d in dims]
    rows.append(f"  → {action}")
    return title, rows


def _panel_stance(prefix, level, action):
    """操作訊號（stance 四元組 → banner）共用格式：BitcoinMonitor/UniversalMonitor 同一份呈現。"""
    return f"{prefix}  {level}", [f"  → {action}"]


def _cut_display(s, width):
    """回傳 (前段 display 寬度<=width, 剩餘字串)。維持 emoji + FE0F 修飾符不被拆開。"""
    out, w, i, n = "", 0, 0, len(s)
    while i < n:
        seg = s[i:i + 2] if (i + 1 < n and s[i + 1] == "️") else s[i]
        cw = _dw(seg)
        if w + cw > width and out:
            break
        out += seg
        w += cw
        i += len(seg)
    return out, s[i:]


_SCORE_PREFIX_RE = re.compile(r"^(\s+[+\-]?\d{1,3}/[±]?\d{1,3}\s+)")
_LIGHTS = "🔴🟢🟡🟠⚪🔵🟣"   # 燈號集合（子項格式「名稱 燈號 描述」的對齊錨點）
_MIN_COL_W = 50   # 兩欄版每欄最小內寬（容標題 + 對齊後最長子項；亦即使用者要的「拉寬」）


def _split_name_light(sub):
    """把子項「名稱 燈號 描述」拆成 (名稱去尾空白, 燈號起的其餘)。找不到燈號回 (None, sub)。"""
    for i, ch in enumerate(sub):
        if ch in _LIGHTS:
            return sub[:i].rstrip(), sub[i:]
    return None, sub


def _row_subitems(r):
    """分數列 → (prefix, [(name, light_desc), ...])；非分數列（→/〔參考〕等無 NN/MM 前綴）回 None。"""
    m = _SCORE_PREFIX_RE.match(r)
    if not m:
        return None
    prefix = m.group(1)
    subs = [x.strip() for x in r[len(prefix):].split("；") if x.strip()]
    return prefix, [_split_name_light(x) for x in subs]


def _panel_name_width(rows):
    """全 panel 分數列所有『有名稱』子項的最大名稱顯示寬 → 燈號對齊欄基準。無則 0。"""
    w = 0
    for r in rows:
        parsed = _row_subitems(r)
        if not parsed:
            continue
        for name, _ in parsed[1]:
            if name:
                w = max(w, _dw(name))
    return w


def _render_score_row(prefix, parts, inner_w, namew):
    """分數列 → 每子項各自一行、名稱右補到 namew 使燈號對齊到同一欄。描述超寬則續行硬切。"""
    indent = " " * _dw(prefix)
    deep = " " * (_dw(prefix) + namew + 1)
    out = []
    for k, (name, rest) in enumerate(parts):
        lead = prefix if k == 0 else indent
        line = f"{lead}{name}{' ' * (namew - _dw(name))} {rest}"
        if _dw(line) <= inner_w:
            out.append(line)
        else:
            head, remain = _cut_display(line, inner_w)
            out.append(head)
            while remain:
                head, remain = _cut_display(deep + remain, inner_w)
                out.append(head)
    return out


def _wrap_display(s, width, cont_indent=None):
    """依顯示寬度把 s 折到 width 內。非分數列（→操作建議、〔參考〕、礦工/籌碼說明、即時行情）用：
    在 空白/；/｜/、/，/：/） 後貪婪斷行，單一片段仍超寬則逐字硬切；cont_indent 自動偵測分數前綴
    寬度、非分數列退回 5 格。（分數列的燈號對齊改由 _panel_block/_render_score_row 於面板層處理。）"""
    if _dw(s) <= width:
        return [s]
    m = _SCORE_PREFIX_RE.match(s)
    if cont_indent is None:
        cont_indent = _dw(m.group(1)) if m else 5
    # 1) 切成「可斷行片段」（分隔符留在片段尾；含中文標點斷點，長句才不會硬切在字中間）
    chunks, cur = [], ""
    for ch in s:
        cur += ch
        if ch in "；｜、，：）」》 　":
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    # 2) 過長片段逐字硬切成 <=width
    pieces = []
    for c in chunks:
        while _dw(c) > width:
            head, c = _cut_display(c, width)
            pieces.append(head)
        if c:
            pieces.append(c)
    # 3) 貪婪打包，續行懸掛縮排
    lines, line = [], ""
    for p in pieces:
        if not line:
            line = p
        elif _dw(line + p) <= width:
            line += p
        else:
            lines.append(line)
            ip = " " * cont_indent + p
            line = ip if _dw(ip) <= width else p
    if line:
        lines.append(line)
    return lines


def _panel_block(title, rows, inner_w):
    """單一面板 → 完整框線 block（title + 內容行 + └──┘）。各欄由上往下緊貼排。

    分數列（有名稱子項的）→ 每子項各自一行、名稱補齊使燈號對齊到全 panel 同一欄
    （namew 取全 panel 最長名稱）；其餘列（→操作、〔參考〕、礦工、趨勢無名稱列）走一般換行。"""
    block = [_title(title, inner_w)]
    namew = _panel_name_width(rows)
    for r in rows:
        parsed = _row_subitems(r)
        if namew and parsed and parsed[1] and all(name is not None for name, _ in parsed[1]):
            segs = _render_score_row(parsed[0], parsed[1], inner_w, namew)
        else:
            segs = _wrap_display(r, inner_w)
        for seg in segs:
            block.append(_row(seg, inner_w))
    block.append(_edge("└", "─", "┘", inner_w))
    return block


def _pair_lines(pa, pb, wl, wr):
    """兩面板 (title, rows) 左右並排 → 回傳合併後的字串陣列（不印）。**各欄獨立、由上往下
    緊貼**（不做跨欄逐列同步，否則一欄某列換多行、另一欄就得插空行對齊 → 面板中間出現空行，
    且會撐高、與「一頁看完」衝突）。只把較矮那欄用空列補在**最底部**（└──┘ 之前），讓兩欄
    底框對齊即可。供 render() 需要再往右接 K 線欄時取用。"""
    a = _panel_block(pa[0], pa[1], wl)
    b = _panel_block(pb[0], pb[1], wr)
    h = max(len(a), len(b))
    # 空列插在各自 └──┘（最後一列）之前 → 內容緊貼、空白落在面板底部
    a = a[:-1] + [_row("", wl)] * (h - len(a)) + [a[-1]]
    b = b[:-1] + [_row("", wr)] * (h - len(b)) + [b[-1]]
    return [la + lb for la, lb in zip(a, b)]


# ── K 線圖（右側全高側欄，2026-07 新增）───────────────────────────────────────
# 只用 _daily_cache 既有的日線 OHLC（零額外網路請求），畫成每日 1 字元寬欄位的蠟燭。
# ANSI 顏色碼刻意不進 _dw()/_row() 的版寬計算（那條路是給全形/emoji 走的，escape
# char 會被當成一般窄字元誤加寬度、撐壞對齊）→ 顏色只在「已量好純文字寬度」之後
# 包住字元，畫面可見寬度不受影響；含色碼的列改用手動 "│" + content + "│" 组框，
# 不走 _row()。標題/日期軸/底框仍是純文字，照舊用 _title/_row/_edge。
_ANSI_GREEN = "\x1b[92m"
_ANSI_RED = "\x1b[91m"
_ANSI_CYAN = "\x1b[96m"
_ANSI_YELLOW = "\x1b[93m"
_ANSI_BLUE = "\x1b[94m"
_ANSI_MAGENTA = "\x1b[95m"
_ANSI_RESET = "\x1b[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── K 線疊圖均線：(天期, 繪圖字元, ANSI 色) ──────────────────────────────────
#
# ⚠ 每條線的字元刻意都不一樣，不是只靠顏色區分：`_enable_windows_ansi` 對舊主控台是
#   **靜默降級**（抓不到 handle 就 pass），那時整組顏色失效，同字元就變成完全分不出來。
#
# ⚠ **分市場不同組，因為「根/年」不同**（實測 Yahoo 10y 日線：台股 243、美股 251、幣 365）：
#   同一個 MA200 在股市涵蓋 9.6~9.9 個月、在幣市只有 6.5 個月，根本不是同一件事；
#   MA20 在股市 0.89 個月（台股正是「月線」），在幣市只剩 0.62 個月（不是月線）。
#   拿固定根數當固定時長，對 24/7 市場一律會錯。
_MA5 = (5, "·", _ANSI_CYAN)
_MA20 = (20, "-", _ANSI_YELLOW)
_MA60 = (60, "+", _ANSI_BLUE)
_MA200 = (200, "=", _ANSI_MAGENTA)
_MA240 = (240, "=", _ANSI_MAGENTA)

# 台股：243 根/年 → **年線是 240 不是 200**（200 是美股移植數字）；中期看季線 60。
# 選 60 而非 120/200 還有一個實證理由：8 檔 × 10 年逐日，落在近 30 日 K 線框內的比例
# MA60 為 94%、MA120 59%、MA200 39%、MA240 34% —— 季線是真的會跟價格互動的那條，
# 年線多數時候只能靠圖例讀值（框外不畫線，見 `_ma_legend_segs`）。
KLINE_MAS_TW = (_MA5, _MA20, _MA60, _MA240)
# 美股／幣對（亦為未知市場的預設）：沿用 5/20/200 國際慣例。
KLINE_MAS = (_MA5, _MA20, _MA200)
_KLINE_MAS_BY_KIND = {"tw_stock": KLINE_MAS_TW}


def kline_mas_for(kind):
    """市場類別（`classify_symbol` 的 `kind`）→ 該市場的疊圖均線組。
    未知類別回國際慣例組，不拋錯——側欄是輔助顯示，不該因為新增市場別就整頁掛掉。"""
    return _KLINE_MAS_BY_KIND.get(kind, KLINE_MAS)


def _dw_ansi(s):
    """含 ANSI 色碼字串的顯示寬度（色碼不佔格）。`_dw` 會把 ESC[ 92m 逐字元算進去，
    K 線列/圖例列都內嵌色碼 → 對齊與終端機寬度判斷一律走這個。"""
    return _dw(_ANSI_RE.sub("", s))


def _enable_windows_ansi():
    """Windows 主控台預設不解讀 ANSI escape，需開 ENABLE_VIRTUAL_TERMINAL_PROCESSING。
    僅 Windows 需要；抓不到 handle／舊主控台等任何失敗一律靜默忽略——K 線退化為
    無色但仍可讀（body/wick 字元本身已分得出來），不影響其餘畫面。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


_enable_windows_ansi()


def _fmt_axis_price(v):
    """K 線圖 y 軸標籤：BitcoinMonitor 也被非 BTC 幣對複用（is_btc=False），價格量級
    可能差很多（ETH 數千 vs 小市值幣 <1），依量級決定小數位數，避免小額幣種顯示成 0。"""
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.4f}"


def _kline_column(o, h, l, c, height, pmin, pmax):
    """單日 OHLC → 該日欄位每一列 (char, color)，row 0 在最上方（對應 pmax）。"""
    span = (pmax - pmin) or 1.0

    def to_row(price):
        frac = (price - pmin) / span
        return min(max(int(round((1 - frac) * (height - 1))), 0), height - 1)

    r_hi, r_lo = to_row(h), to_row(l)
    body_top, body_bot = sorted((to_row(o), to_row(c)))
    color = _ANSI_GREEN if c >= o else _ANSI_RED
    col = [None] * height
    for r in range(r_hi, r_lo + 1):
        col[r] = ("█" if body_top <= r <= body_bot else "│", color)
    return col


def _ma_series(df, n_days, mas):
    """近 n_days 日的各均線值 → [(period, char, color, values)]，values 長度＝n_days。
    歷史不足以讓**整段 n_days 都有值**的天期整條略過——畫半截均線比不畫更誤導
    （會看起來像「均線在這天才開始」）。`close` 缺欄/df 太短一律回 []。"""
    if df is None or "close" not in getattr(df, "columns", []):
        return []
    out = []
    for period, ch, color in mas:
        if len(df) < period + n_days - 1:
            continue
        vals = df["close"].rolling(period).mean().iloc[-n_days:].tolist()
        if any(v != v for v in vals):        # NaN（close 有缺）→ 該天期不畫
            continue
        out.append((period, ch, color, vals))
    return out


def _ma_legend_segs(ma_data, close, pmin, pmax):
    """均線圖例片段 → [(plain, colored)]，plain 供排版量寬、colored 供輸出。

    每段格式 `·MA5 209.8 +1.1%`：字元＋天期＋均線現值＋**收盤價對它的乖離**。
    落在 [pmin, pmax] 框外的均線末尾加 `↑`/`↓` 表示「線在圖框上/下方，未畫出」。

    為何框外不 clamp 到邊緣：實測 8 檔 × 10 年逐日，MA 落在近 30 日框內的比例為
    MA5/10/20 100%、MA60 94%、MA120 59%、**MA200 僅 39%**、MA240 34%——MA200 有六成
    交易日在框外，硬貼邊會讓「年線遠在下方 23%」看起來像「價格剛好站在年線上」，
    是會直接害人誤判的假訊號。改成不畫線但圖例照給數值與乖離，資訊一點不少。"""
    segs = []
    for period, ch, color, vals in ma_data:
        v = vals[-1]
        dev = (close / v - 1) * 100 if v else 0.0
        mark = "" if pmin <= v <= pmax else ("↑" if v > pmax else "↓")
        plain = f"{ch}MA{period} {_fmt_axis_price(v)} {dev:+.1f}%{mark}"
        segs.append((plain, f"{color}{plain}{_ANSI_RESET}"))
    return segs


def _pack_segs(segs, width, sep="  "):
    """把 [(plain, colored)] 依 plain 寬度裝進每列不超過 width 的多列 → [(plain, colored)]。
    單段本身就超寬時該段自成一列、不截斷，由呼叫端以實際寬度撐開面板。
    `segs` 為空回 []（呼叫端不必自己先擋空）。"""
    lines, cur_p, cur_c = [], "", ""
    for plain, colored in segs:
        cand = plain if not cur_p else cur_p + sep + plain
        if cur_p and _dw(cand) > width:
            lines.append((cur_p, cur_c))
            cur_p, cur_c = plain, colored
        else:
            cur_p, cur_c = cand, (colored if not cur_c else cur_c + sep + colored)
    if cur_p:
        lines.append((cur_p, cur_c))
    return lines


def _kline_panel_lines(df, n_days, height, mas=KLINE_MAS):
    """近 n_days 日 K 線（疊 `mas` 指定的均線）→ 完整框線 block（┌─┐/│…│/└─┘，
    與其他面板同款式），高度＝呼叫端指定的 height（供逐列跟左欄整頁並排），
    每日固定佔 1 字元寬欄位。
    資料不足 / 可畫列數太少（<5）時回 []，呼叫端據此退回舊版單欄（不並排）畫面。

    ⚠ 疊圖優先序：**K 棒 > 短天期均線 > 長天期均線**（同一格只放得下一個字元）。
    K 棒永遠贏——它是價格本身，被均線蓋掉就讀不出當天漲跌了；均線的關鍵資訊本來就是
    它**沒被 K 棒蓋住**的那段（在價格上方＝壓力、下方＝支撐）。
    面板寬度取「軸標籤+天數」與「圖例列」的較大者，圖例才不會撐破右框線。"""
    if df is None or len(df) < n_days or height < 8:
        return []
    sub = df.iloc[-n_days:]
    pmax = float(sub["high"].max())
    pmin = float(sub["low"].min())
    if not (pmax > pmin):
        return []

    tick_prices = [pmin + i * (pmax - pmin) / 3 for i in range(4)]
    label_w = max(len(_fmt_axis_price(p)) for p in tick_prices) + 1   # +1：軸刻度符
    grid_w = label_w + n_days

    ma_data = _ma_series(df, n_days, mas)
    segs = _ma_legend_segs(ma_data, float(sub["close"].iloc[-1]), pmin, pmax)
    legend = _pack_segs(segs, grid_w)
    # 扣：標題列 + 圖例列 + 日期軸列 + 底框
    n_rows = height - 3 - len(legend)
    if n_rows < 5:
        return []
    inner_w = max(grid_w, max((_dw(p) for p, _ in legend), default=0))

    def _row_of(price):
        frac = (price - pmin) / (pmax - pmin)
        return min(max(int(round((1 - frac) * (n_rows - 1))), 0), n_rows - 1)

    tick_rows = {_row_of(p): p for p in tick_prices}
    cols = [_kline_column(float(r.open), float(r.high), float(r.low), float(r.close),
                          n_rows, pmin, pmax) for r in sub.itertuples()]
    # 均線層：反向走訪 → 短天期後寫入、蓋掉長天期（見 docstring 優先序）
    ma_layer = [[None] * n_rows for _ in range(n_days)]
    for period, ch, color, vals in reversed(ma_data):
        for i, v in enumerate(vals):
            if pmin <= v <= pmax:
                ma_layer[i][_row_of(v)] = (ch, color)

    lines = [_title(f"K 線圖  近{n_days}日（綠漲／紅跌）", inner_w)]
    grid_pad = " " * (inner_w - grid_w)      # inner_w ≥ grid_w（見上），故不必 max(0, …)
    for r in range(n_rows):
        if r in tick_rows:
            label = f"{_fmt_axis_price(tick_rows[r]):>{label_w - 1}}┤"
        else:
            label = " " * (label_w - 1) + "│"
        day_chars = []
        for i, col in enumerate(cols):
            cell = col[r] or ma_layer[i][r]       # K 棒優先，空格才讓給均線
            day_chars.append(" " if cell is None else f"{cell[1]}{cell[0]}{_ANSI_RESET}")
        body = label + "".join(day_chars)
        lines.append("│" + body + grid_pad + "│")
    for plain, colored in legend:
        lines.append("│" + colored + " " * (inner_w - _dw(plain)) + "│")
    d0 = sub.index[0].strftime("%m/%d")
    d1 = sub.index[-1].strftime("%m/%d")
    gap = max(1, n_days - len(d0) - len(d1))
    xaxis = (" " * label_w + d0 + " " * gap + d1)[:inner_w].ljust(inner_w)
    lines.append(_row(xaxis, inner_w))
    lines.append(_edge("└", "─", "┘", inner_w))
    return lines


def _print_with_kline(left, W, df, n_days, enabled=True, mas=KLINE_MAS):
    """左欄整頁字串陣列 + 日線 df → 右接全高 K 線側欄後印出（BitcoinMonitor 與
    watcher.UniversalMonitor 共用單一來源）。側欄畫不出（資料不足/終端機太窄/enabled=False）
    → 原樣逐行印，行為與無側欄版完全相同。
    `mas` 為疊圖均線組，呼叫端依市場類別傳（見 `kline_mas_for`）；預設國際慣例組。"""
    try:
        term_cols = os.get_terminal_size().columns
    except OSError:
        term_cols = None
    chart = _kline_panel_lines(df, n_days, len(left), mas=mas) if enabled else []
    # 終端機寬度用**側欄實寬**判斷，不用固定 +45 猜：疊上均線後圖例列可能比格線寬
    # （長天期均線值位數多），固定門檻會讓側欄溢出換行、整頁對齊全毀。
    # W+2＝左欄框線實寬（_row/_edge 為「│+內容W+│」），+2＝兩欄之間的分隔空白。
    if chart and term_cols is not None:
        chart_w = max(_dw_ansi(c) for c in chart)
        if term_cols < W + 2 + 2 + chart_w:
            chart = []
    if not chart:
        for l in left:
            print(l)
        return
    h = max(len(left), len(chart))
    left = left + [""] * (h - len(left))
    chart = chart + [""] * (h - len(chart))
    # 左欄定寬＝框線列實寬（_row/_edge 為「│+內容W+│」= W+2）；空白分隔列/尾列
    # 不足此寬 → 補滿，否則右側 K 線在那幾列會縮到最左
    lw = max(_dw(l) for l in left)
    for l, r in zip(left, chart):
        print(l + " " * max(0, lw - _dw(l)) + "  " + r)


def interruptible_wait(seconds, nav=False):
    """
    等待 seconds 秒。nav=True（由 watcher 進入）時偵測鍵盤指令並提早返回：
      b / Enter → 'back'（回上層重選代號）；q → 'quit'（結束）；
      e → 'exec'（E3 執行標記：UniversalMonitor 記日誌後繼續；本檔 run 忽略）。
    回傳指令字串或 None。
    nav=False（BTC_WATCH 單獨執行）或非 Windows 無 msvcrt → 純 sleep、不收指令（行為不變）。
    """
    if not nav:
        time.sleep(seconds)
        return None
    try:
        import msvcrt
    except ImportError:
        time.sleep(seconds)
        return None
    end = time.time() + seconds
    while time.time() < end:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"b", b"B", b"\r", b"\n"):
                return "back"
            if ch in (b"q", b"Q"):
                return "quit"
            if ch in (b"e", b"E"):
                return "exec"
        time.sleep(0.1)
    return None
