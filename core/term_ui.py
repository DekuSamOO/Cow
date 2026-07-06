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


# 文字呈現預設的窄符號：在 emoji 範圍內、但終端機（無 FE0F 時）渲染為寬度 1
# ⚠ U+26A0 警告號。其餘 emoji（⚪🔴🟡…）維持寬度 2。
_NARROW_SYMBOLS = {0x26A0}


def _dw(s):
    """字串顯示寬度：全形/emoji=2、半形=1（FE0F 修飾符=0）；窄符號（⚠）=1。"""
    w = 0
    for ch in s:
        if ch == "️":
            continue
        o = ord(ch)
        if o in _NARROW_SYMBOLS:
            w += 1
        elif 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF:
            w += 2
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
_ANSI_RESET = "\x1b[0m"


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


def _kline_panel_lines(df, n_days, height):
    """近 n_days 日 K 線 → 完整框線 block（┌─┐/│…│/└─┘，與其他面板同款式），
    高度＝呼叫端指定的 height（供逐列跟左欄整頁並排），每日固定佔 1 字元寬欄位。
    資料不足 / 可畫列數太少（<5）時回 []，呼叫端據此退回舊版單欄（不並排）畫面。"""
    if df is None or len(df) < n_days or height < 8:
        return []
    sub = df.iloc[-n_days:]
    pmax = float(sub["high"].max())
    pmin = float(sub["low"].min())
    if not (pmax > pmin):
        return []
    n_rows = height - 3   # 扣：標題列 + 日期軸列 + 底框
    if n_rows < 5:
        return []

    tick_prices = [pmin + i * (pmax - pmin) / 3 for i in range(4)]
    label_w = max(len(_fmt_axis_price(p)) for p in tick_prices) + 1   # +1：軸刻度符

    def _row_of(price):
        frac = (price - pmin) / (pmax - pmin)
        return min(max(int(round((1 - frac) * (n_rows - 1))), 0), n_rows - 1)

    tick_rows = {_row_of(p): p for p in tick_prices}
    cols = [_kline_column(float(r.open), float(r.high), float(r.low), float(r.close),
                          n_rows, pmin, pmax) for r in sub.itertuples()]

    inner_w = label_w + n_days
    lines = [_title(f"K 線圖  近{n_days}日（綠漲／紅跌）", inner_w)]
    for r in range(n_rows):
        if r in tick_rows:
            label = f"{_fmt_axis_price(tick_rows[r]):>{label_w - 1}}┤"
        else:
            label = " " * (label_w - 1) + "│"
        day_chars = []
        for col in cols:
            cell = col[r]
            day_chars.append(" " if cell is None else f"{cell[1]}{cell[0]}{_ANSI_RESET}")
        lines.append("│" + label + "".join(day_chars) + "│")
    d0 = sub.index[0].strftime("%m/%d")
    d1 = sub.index[-1].strftime("%m/%d")
    gap = max(1, n_days - len(d0) - len(d1))
    xaxis = (" " * label_w + d0 + " " * gap + d1)[:inner_w].ljust(inner_w)
    lines.append(_row(xaxis, inner_w))
    lines.append(_edge("└", "─", "┘", inner_w))
    return lines


def _print_with_kline(left, W, df, n_days, enabled=True):
    """左欄整頁字串陣列 + 日線 df → 右接全高 K 線側欄後印出（BitcoinMonitor 與
    watcher.UniversalMonitor 共用單一來源）。側欄畫不出（資料不足/終端機太窄/enabled=False）
    → 原樣逐行印，行為與無側欄版完全相同。"""
    try:
        term_cols = os.get_terminal_size().columns
    except OSError:
        term_cols = None
    show = enabled and (term_cols is None or term_cols >= W + 45)
    chart = _kline_panel_lines(df, n_days, len(left)) if show else []
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
