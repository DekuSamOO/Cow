"""
core/watch_plan.py · 交易計畫檔（watch_plan.json）載入/驗證/衍生計算 — 純函數、零網路

波段執行可靠度第一槓桿：進場區/停損/目標**先落檔**，盤中照表操課、不臨場起意
（設計文件：Obsidian\\Github\\Cow\\20260704plan_watcher波段執行.md §E1）。

watch_plan.json 放 repo 根目錄、**必須在 .gitignore**（Cow 為公開 repo，個人部位計畫不得入庫）。
格式（key 用 watcher 輸入代號同語彙：2330 / QQQ / BTCUSDT，比對前一律 upper）：

{
  "2330": {"direction": "long", "entry": [950, 970], "stop": 920,
            "targets": [1050, 1120], "size_pct": 15,
            "valid_until": "2026-07-31", "note": "回踩月線佈局"}
}

驗證規則（載入即擋，壞計畫不進面板/警戒——寧缺勿錯）：
  long ：stop < entry_low ≤ entry_high < targets（遞增）
  short：stop > entry_high ≥ entry_low > targets（遞減）
`valid_until`（選填）過期 → 面板標註且警戒引擎（E2）不觸發：過期計畫照響＝雜訊。
"""
import dataclasses
import datetime
import json
import os
from typing import Optional, Tuple, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLAN_PATH = os.path.join(_REPO_ROOT, "watch_plan.json")

# mtime 快取：watcher 每 60s render 都會查計畫（支援盤中改檔即生效），檔案沒動不重讀
_cache = {"path": None, "mtime": None, "plans": {}, "errors": []}


@dataclasses.dataclass(frozen=True)
class TradePlan:
    symbol: str
    direction: str                    # "long" / "short"
    entry_low: float
    entry_high: float
    stop: float
    targets: Tuple[float, ...]
    size_pct: Optional[float] = None
    valid_until: Optional[datetime.date] = None
    note: str = ""

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_mid - self.stop)

    def r_multiple(self) -> Optional[float]:
        """第一目標的風報比 R＝獲利距離÷停損距離（以進場區中點計）。"""
        if not self.targets or self.risk_per_unit <= 0:
            return None
        reward = (self.targets[0] - self.entry_mid if self.direction == "long"
                  else self.entry_mid - self.targets[0])
        return reward / self.risk_per_unit

    def expired(self, today: Optional[datetime.date] = None) -> bool:
        if self.valid_until is None:
            return False
        return (today or datetime.date.today()) > self.valid_until


def _parse_one(symbol: str, raw: dict) -> TradePlan:
    """單一代號計畫解析＋驗證；不合法拋 ValueError（訊息含代號，供 load_plans 收集）。"""
    try:
        direction = str(raw["direction"]).lower()
        entry = raw["entry"]
        entry_low, entry_high = float(min(entry)), float(max(entry))
        stop = float(raw["stop"])
        targets = tuple(float(t) for t in raw.get("targets", []))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{symbol}: 欄位缺漏或型別錯誤（{e}）") from e
    if direction not in ("long", "short"):
        raise ValueError(f"{symbol}: direction 須為 long/short（收到 {direction!r}）")

    if direction == "long":
        ok = stop < entry_low and (not targets or (entry_high < targets[0]
                                                   and list(targets) == sorted(targets)))
        rule = "long 須 stop < entry ≤ targets 遞增"
    else:
        ok = stop > entry_high and (not targets or (entry_low > targets[0]
                                                    and list(targets) == sorted(targets, reverse=True)))
        rule = "short 須 stop > entry ≥ targets 遞減"
    if not ok:
        raise ValueError(f"{symbol}: 價位順序不合法（{rule}）")

    valid_until = None
    if raw.get("valid_until"):
        try:
            valid_until = datetime.date.fromisoformat(str(raw["valid_until"]))
        except ValueError as e:
            raise ValueError(f"{symbol}: valid_until 須為 YYYY-MM-DD") from e

    return TradePlan(symbol=symbol.upper(), direction=direction,
                     entry_low=entry_low, entry_high=entry_high, stop=stop, targets=targets,
                     size_pct=(float(raw["size_pct"]) if raw.get("size_pct") is not None else None),
                     valid_until=valid_until, note=str(raw.get("note", "")))


def load_plans(path: Optional[str] = None) -> Tuple[Dict[str, TradePlan], List[str]]:
    """讀取整份計畫檔 → ({symbol: TradePlan}, [錯誤訊息])。
    檔案不存在＝正常（沒寫計畫），回空；JSON 壞掉/個別計畫不合法 → 收進 errors 不拋
    （watcher 顯示警示行即可，監控不因計畫檔打錯字而中斷）。"""
    path = path or DEFAULT_PLAN_PATH
    if not os.path.exists(path):
        return {}, []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {}, [f"watch_plan.json 解析失敗：{e}"]
    plans, errors = {}, []
    for sym, raw in data.items():
        try:
            plans[sym.upper()] = _parse_one(sym, raw)
        except ValueError as e:
            errors.append(str(e))
    return plans, errors


def load_plans_cached(path: Optional[str] = None) -> Tuple[Dict[str, TradePlan], List[str]]:
    """mtime 快取版：檔案沒動不重讀（watcher 60s 輪詢用；盤中改檔下一輪即生效）。"""
    path = path or DEFAULT_PLAN_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _cache["path"] != path or _cache["mtime"] != mtime:
        plans, errors = load_plans(path)
        _cache.update(path=path, mtime=mtime, plans=plans, errors=errors)
    return _cache["plans"], _cache["errors"]


def get_plan(symbol: str, path: Optional[str] = None) -> Optional[TradePlan]:
    plans, _ = load_plans_cached(path)
    return plans.get((symbol or "").upper())


def _pct(level: float, price: float) -> str:
    return f"{(level / price - 1) * 100:+.1f}%"


def plan_panel_rows(plan: TradePlan, price: float, fmt=lambda v: f"{v:,.2f}",
                    today: Optional[datetime.date] = None) -> List[str]:
    """「交易計畫」面板內容列（與 watcher 其餘面板同縮排慣例，兩空格開頭）。
    price 用當下有效價（有即時報價用即時、否則日線收盤），距離一律相對現價。"""
    rows = []
    if plan.expired(today):
        rows.append(f"  ⚠ 計畫已過期（{plan.valid_until}）——警戒不觸發，請更新 watch_plan.json")
    size = f"  倉位 {plan.size_pct:.0f}%" if plan.size_pct is not None else ""
    valid = f"（{plan.valid_until} 前有效）" if plan.valid_until else ""
    rows.append(f"  方向          {plan.direction.upper()}{size}{valid}")
    rows.append(f"  進場區        {fmt(plan.entry_low)} ~ {fmt(plan.entry_high)}"
                f"   （下緣距現價 {_pct(plan.entry_low, price)}）")
    rows.append(f"  停損          {fmt(plan.stop)}   （距現價 {_pct(plan.stop, price)}）")
    if plan.targets:
        tgt = " / ".join(fmt(t) for t in plan.targets)
        r = plan.r_multiple()
        r_txt = f"｜R {r:.1f}" if r is not None else ""
        rows.append(f"  目標          {tgt}   （T1 距現價 {_pct(plan.targets[0], price)}{r_txt}）")
    if plan.note:
        rows.append(f"  備註          {plan.note}")
    return rows
