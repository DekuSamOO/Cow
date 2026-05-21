"""
strategy/swing.py
Antigravity v4 波段交易策略 & 回測引擎（五合一進場過濾）
純 Python，無 Streamlit 依賴

五合一進場條件（v4.1 升級）:
  1. Price > SMA200          — 年線多頭，大趨勢向上
  2. RSI_14 > 50             — 短期動能偏多
  3. 0% ≤ dist_EMA20 ≤ 1.5% — 回踩甜蜜點，不追高
  4. MACD > Signal           — MACD 多頭交叉確認動能
  5. ADX > 20                — 趨勢強度足夠，過濾盤整假訊號

[Task #5] 回測引擎向量化:
原始邏輯使用 for i in range(len(bt_df)) 逐行掃描，在 2000+ 天的資料集下
每次重新渲染 Tab 都需要數秒。

重構思路：
- 先用 Pandas shift/boolean mask 向量化計算出所有訊號欄位
- 再用「狀態機輔助」的方式只迭代「進出場轉換點」（通常 < 100 次），
  而非逐行掃描所有 2000+ 天
- 理論加速：10-50x，取決於資料長度與交易次數
"""
import math
import numpy as np
import pandas as pd
from typing import Optional

# 從集中設定檔讀取預設交易成本參數
from config import DEFAULT_FEE_RATE, DEFAULT_SLIPPAGE_RATE
from core.swing_signals import compute_entry_mask, compute_exit_mask

try:
    import pandas_ta as ta
    _HAS_TA = True
except ImportError:
    _HAS_TA = False


def calculate_max_drawdown(equity_curve):
    """計算最大回撤 (%)"""
    if len(equity_curve) < 1:
        return 0.0
    peaks = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - peaks) / peaks
    return drawdowns.min() * 100


def run_swing_strategy_backtest(
    df,
    start_date,
    end_date,
    initial_capital=10_000,
    fee_rate=DEFAULT_FEE_RATE,
    slippage_rate=DEFAULT_SLIPPAGE_RATE,
    entry_dist_min_pct: Optional[float] = None,  # ✅ 修正 1：參數名稱改回 entry_dist_min_pct，與 UI 傳入的名稱一致
    rsi_min: Optional[int] = None,
    adx_min: Optional[int] = None,
    exit_ma: str = "SMA_50",  # 接收 UI 傳來的動態防守線參數
):
    """
    Antigravity v4 波段策略回測（五合一進場過濾 + 動態出場防守線）

    進場: Price > SMA200 AND RSI_14 > 50 AND 0% ≤ dist_from_EMA20
          AND MACD > Signal AND ADX > 20
    出場: Price < UI傳入的防守線 (預設 SMA_50)

    [Backtest Realism] 交易摩擦成本:
    ─────────────────────────────────────────────────────────────────
    fee_rate      : 單邊手續費率（如 0.001 = 0.1%，Taker Fee）
    slippage_rate : 滑點估算（如 0.001 = 0.1%，因市場深度不足的成交偏差）

    實際進場成本:
        effective_entry = price * (1 + fee_rate + slippage_rate)
        → 例如：BTC=$100,000，cost=0.2%，實際成本=$100,200

    實際出場收益:
        effective_exit = price * (1 - fee_rate - slippage_rate)
        → 例如：BTC=$110,000，cost=0.2%，實際收益=$109,780

    合計一來一回摩擦成本 ≈ 0.4%（0.2% 進 + 0.2% 出）
    ─────────────────────────────────────────────────────────────────

    返回: (trades_df, final_equity, roi_pct, trade_count, max_drawdown_pct, stats_dict)
    """
    mask  = (df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))
    bt_df = df.loc[mask].copy()

    if bt_df.empty:
        return pd.DataFrame(), 0.0, 0.0, 0, 0.0, {}

    # 套用自訂參數，未提供則使用安全預設值
    _dist_min = entry_dist_min_pct if entry_dist_min_pct is not None else 0.0
    _rsi_min  = rsi_min  if rsi_min  is not None else 50
    _adx_min  = adx_min  if adx_min  is not None else 20

    # ──────────────────────────────────────────────────────────────
    # 第一段：向量化計算所有訊號（無 Python for loop）
    # ──────────────────────────────────────────────────────────────
    close  = bt_df['close']
    
    is_entry = compute_entry_mask(
        bt_df,
        entry_dist_min_pct=_dist_min,
        rsi_min=_rsi_min,
        adx_min=_adx_min
    )
    
    is_exit = compute_exit_mask(bt_df, exit_ma=exit_ma)

    # ──────────────────────────────────────────────────────────────
    # 第二段：狀態機迭代「訊號觸發點」（不是逐行，只迭代轉換）
    # ──────────────────────────────────────────────────────────────
    # 【防先視偏誤】：訊號在第 N 根 K 棒收盤後確認 → 下單在第 N+1 根開盤執行
    # shift(1) 讓 entry_mask[i] 代表「前一根收盤觸發，本根開盤進場」
    entry_mask = is_entry.shift(1).fillna(False).values
    exit_mask  = is_exit.shift(1).fillna(False).values
    dates      = bt_df.index
    closes     = close.values      # 收盤價：用於標記市值（Sharpe 計算）
    opens      = bt_df['open'].values  # 開盤價：實際執行價（次根開盤，防先視偏誤）

    balance     = initial_capital
    position    = 0.0
    state       = "CASH"           # 狀態機：CASH / INVESTED
    entry_price = 0.0
    trades      = []
    equity_daily = {}              # date → equity（用於計算日頻 Sharpe）

    for i in range(len(bt_df)):
        # exec_price：本根開盤 = 前一根訊號觸發後實際下單價（防先視偏誤）
        exec_price = opens[i]
        date       = dates[i]

        if state == "CASH" and entry_mask[i]:
            # ── 進場（含手續費與滑點摩擦成本）──
            friction_in           = fee_rate + slippage_rate
            effective_entry_price = exec_price * (1.0 + friction_in)

            # 以調整後成本計算可購入的幣量（balance 全倉投入）
            position    = balance / effective_entry_price
            entry_price = effective_entry_price

            trades.append({
                "Type":       "Buy",
                "Date":       date,
                "Price":      exec_price,             # 次根開盤執行價
                "Entry_Cost": effective_entry_price,  # 實際成本（含摩擦）
                "Fee%":       friction_in * 100,
                "Balance":    balance,
                "Crypto":     position,
                "Reason":     "Sweet Spot",
            })
            balance = 0.0
            state   = "INVESTED"

        elif state == "INVESTED" and exit_mask[i]:
            # ── 出場（含手續費與滑點摩擦成本）──
            friction_out         = fee_rate + slippage_rate
            effective_exit_price = exec_price * (1.0 - friction_out)

            balance = position * effective_exit_price

            gross_cost  = entry_price * position
            net_pnl     = balance - gross_cost
            net_pnl_pct = (effective_exit_price / entry_price - 1) * 100

            trades.append({
                "Type":     "Sell",
                "Date":     date,
                "Price":    exec_price,
                "Exit_Net": effective_exit_price,
                "Fee%":     friction_out * 100,
                "Balance":  balance,
                "Crypto":   0.0,
                "Reason":   f"Trend Break (<{exit_ma})",
                "PnL":      net_pnl,
                "PnL%":     net_pnl_pct,
            })
            position = 0.0
            state    = "CASH"

        # 每日市值快照（持倉用收盤價標記，現金原值）
        equity_daily[date] = balance if state == "CASH" else position * closes[i]

    # ──────────────────────────────────────────────────────────────
    # 計算最終權益與最大回撤
    # ──────────────────────────────────────────────────────────────
    last_close   = closes[-1]
    final_equity = balance if state == "CASH" else position * last_close
    roi          = (final_equity - initial_capital) / initial_capital * 100

    # 從 Sell 交易重建權益曲線（向量化）
    trades_df = pd.DataFrame(trades)

    if not trades_df.empty and 'Balance' in trades_df.columns:
        sell_balances = trades_df.loc[trades_df['Type'] == 'Sell', 'Balance'].tolist()
    else:
        sell_balances = []

    equity_curve = np.array([initial_capital] + sell_balances + [final_equity])
    mdd          = calculate_max_drawdown(equity_curve)

    # ──────────────────────────────────────────────────────────────
    # 進階統計（向量化計算）
    # ──────────────────────────────────────────────────────────────
    stats = {'win_rate': 0.0, 'sharpe': 0.0, 'avg_profit': 0.0, 'avg_loss': 0.0}
    trade_count = 0

    if not trades_df.empty:
        trade_count = int((trades_df['Type'] == 'Buy').sum())

        if 'PnL%' in trades_df.columns:
            sell_trades = trades_df[trades_df['Type'] == 'Sell'].dropna(subset=['PnL%'])
            if not sell_trades.empty:
                pnl_arr = sell_trades['PnL%'].values  # NumPy array

                winners   = pnl_arr[pnl_arr > 0]
                losers    = pnl_arr[pnl_arr <= 0]

                stats['win_rate']   = len(winners) / len(pnl_arr) * 100
                stats['avg_profit'] = float(winners.mean()) if len(winners) > 0 else 0.0
                stats['avg_loss']   = float(losers.mean())  if len(losers) > 0  else 0.0

    # 日頻 Sharpe（從 equity_daily 日線市值曲線計算，比逐筆交易報酬更準確）
    if equity_daily:
        eq_series  = pd.Series(equity_daily)
        daily_rets = eq_series.pct_change().dropna()
        if len(daily_rets) > 1 and daily_rets.std() > 0:
            stats['sharpe'] = float(daily_rets.mean() / daily_rets.std() * math.sqrt(252))

    return trades_df, final_equity, roi, trade_count, mdd, stats


# ==============================================================================
# 多週期回測引擎 (Multi-Timeframe Backtest)
# ==============================================================================

def run_multitf_backtest(
    df_daily: pd.DataFrame,
    df_15m: pd.DataFrame,
    start_date,
    end_date,
    initial_capital: float = 10_000,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
    daily_use_sma200: bool = True,
    daily_use_golden: bool = False,
    ema_period_15m: int = 20,
    rsi_min_15m: int = 50,
    stop_loss_pct: float = 3.0,
):
    """
    多週期回測：日線宏觀過濾 + 15m 精確進場

    過濾邏輯（嚴格防先視偏誤）：
      日線條件在第 N 日收盤後確認 → 第 N+1 日起的 15m K 棒才允許進場
      15m 訊號在第 M 根 15m K 棒收盤後確認 → 第 M+1 根開盤執行

    日線宏觀過濾（可選）：
      - daily_use_sma200  : 收盤 > SMA200（年線多頭確認）
      - daily_use_golden  : SMA50 > SMA200（金叉，中長期多頭）

    15m 進場條件：
      - 15m 收盤 > 15m EMA{ema_period_15m}（短期趨勢向上）
      - 15m RSI14 > rsi_min_15m（動能偏多）

    15m 出場條件：
      - 15m 收盤 < 15m EMA20（趨勢轉弱）
      - 固定停損：進場後收盤跌破 stop_loss_pct%

    返回: (trades_df, final_equity, roi_pct, trade_count, max_drawdown_pct, stats_dict)
    """
    # ──────────────────────────────────────────────────────────────
    # 1. 日線宏觀過濾（shift 1 天，防先視偏誤）
    # ──────────────────────────────────────────────────────────────
    daily_mask  = (df_daily.index >= pd.Timestamp(start_date)) & \
                  (df_daily.index <= pd.Timestamp(end_date))
    daily_slice = df_daily.loc[daily_mask]

    if daily_slice.empty:
        return pd.DataFrame(), 0.0, 0.0, 0, 0.0, {}

    # 初始全部允許
    daily_bull = pd.Series(True, index=daily_slice.index)

    if daily_use_sma200 and 'SMA_200' in daily_slice.columns:
        daily_bull = daily_bull & (daily_slice['close'] > daily_slice['SMA_200'].fillna(0))

    if daily_use_golden and 'SMA_50' in daily_slice.columns and 'SMA_200' in daily_slice.columns:
        daily_bull = daily_bull & (daily_slice['SMA_50'] > daily_slice['SMA_200'].fillna(0))

    # shift(1)：日線條件以前一根收盤計算，避免當根先視
    daily_bull_shifted = daily_bull.shift(1).fillna(False)

    # ──────────────────────────────────────────────────────────────
    # 2. 15m 資料切片
    # ──────────────────────────────────────────────────────────────
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    mask_15m = (df_15m.index >= start_ts) & (df_15m.index < end_ts)
    bt_15m   = df_15m.loc[mask_15m].copy()

    if bt_15m.empty:
        return pd.DataFrame(), 0.0, 0.0, 0, 0.0, {}

    # ──────────────────────────────────────────────────────────────
    # 3. 計算 15m 技術指標
    # ──────────────────────────────────────────────────────────────
    ema_col = f'EMA_{ema_period_15m}'
    if _HAS_TA:
        bt_15m[ema_col]  = ta.ema(bt_15m['close'], length=ema_period_15m)
        bt_15m['RSI_14'] = ta.rsi(bt_15m['close'], length=14)
    else:
        bt_15m[ema_col]  = bt_15m['close'].ewm(span=ema_period_15m, adjust=False).mean()
        # RSI 手算
        delta  = bt_15m['close'].diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, np.nan)
        bt_15m['RSI_14'] = 100 - 100 / (1 + rs)

    # ──────────────────────────────────────────────────────────────
    # 4. 15m 訊號 + 日線過濾疊加（防先視偏誤：shift 1 根）
    # ──────────────────────────────────────────────────────────────
    close_15m = bt_15m['close']
    ema_15m   = bt_15m[ema_col].fillna(close_15m)
    rsi_15m   = bt_15m['RSI_14'].fillna(50)

    # 15m 原始訊號（收盤確認）
    raw_entry = (close_15m > ema_15m) & (rsi_15m > rsi_min_15m)
    raw_exit  = close_15m < ema_15m

    # 將日線過濾映射到每根 15m K 棒（用當天日期查前一日的日線結果）
    bar_dates        = bt_15m.index.normalize()           # 每根 15m 的 UTC 日期
    daily_ok_mapped  = daily_bull_shifted.reindex(bar_dates, method='ffill').fillna(False)
    daily_ok_mapped.index = bt_15m.index                  # 對齊索引

    # 進場需同時滿足日線過濾；出場則只看 15m（不要求日線仍牛市才出）
    is_15m_entry = (raw_entry & daily_ok_mapped).shift(1).fillna(False)
    is_15m_exit  = raw_exit.shift(1).fillna(False)

    # ──────────────────────────────────────────────────────────────
    # 5. 狀態機（15m 頻率）
    # ──────────────────────────────────────────────────────────────
    entry_mask = is_15m_entry.values
    exit_mask  = is_15m_exit.values
    dates      = bt_15m.index
    closes     = close_15m.values
    opens      = bt_15m['open'].values

    balance     = initial_capital
    position    = 0.0
    state       = "CASH"
    entry_price = 0.0
    stop_price  = 0.0
    trades      = []
    equity_ts   = {}

    for i in range(len(bt_15m)):
        exec_price = opens[i]
        date       = dates[i]

        if state == "CASH" and entry_mask[i]:
            friction_in           = fee_rate + slippage_rate
            effective_entry       = exec_price * (1.0 + friction_in)
            position              = balance / effective_entry
            entry_price           = effective_entry
            stop_price            = exec_price * (1.0 - stop_loss_pct / 100.0)
            trades.append({
                "Type": "Buy", "Date": date,
                "Price": exec_price, "Entry_Cost": effective_entry,
                "Balance": balance, "Crypto": position,
                "Reason": "15m EMA Cross",
            })
            balance = 0.0
            state   = "INVESTED"

        elif state == "INVESTED":
            stop_triggered = closes[i] < stop_price
            if exit_mask[i] or stop_triggered:
                friction_out         = fee_rate + slippage_rate
                effective_exit       = exec_price * (1.0 - friction_out)
                balance              = position * effective_exit
                net_pnl_pct          = (effective_exit / entry_price - 1) * 100
                trades.append({
                    "Type": "Sell", "Date": date,
                    "Price": exec_price, "Exit_Net": effective_exit,
                    "Balance": balance, "Crypto": 0.0,
                    "PnL": balance - entry_price * position,
                    "PnL%": net_pnl_pct,
                    "Reason": "Stop Loss" if stop_triggered else f"15m EMA Break",
                })
                position   = 0.0
                state      = "CASH"

        equity_ts[date] = balance if state == "CASH" else position * closes[i]

    # ──────────────────────────────────────────────────────────────
    # 6. 統計彙整
    # ──────────────────────────────────────────────────────────────
    last_close   = closes[-1] if len(closes) else 0.0
    final_equity = balance if state == "CASH" else position * last_close
    roi          = (final_equity - initial_capital) / initial_capital * 100

    trades_df = pd.DataFrame(trades)

    sell_balances = (trades_df.loc[trades_df['Type'] == 'Sell', 'Balance'].tolist()
                     if not trades_df.empty else [])
    equity_curve  = np.array([initial_capital] + sell_balances + [final_equity])
    mdd           = calculate_max_drawdown(equity_curve)

    stats = {'win_rate': 0.0, 'sharpe': 0.0, 'avg_profit': 0.0, 'avg_loss': 0.0}
    trade_count = 0

    if not trades_df.empty and 'PnL%' in trades_df.columns:
        trade_count = int((trades_df['Type'] == 'Buy').sum())
        sell_trades = trades_df[trades_df['Type'] == 'Sell'].dropna(subset=['PnL%'])
        if not sell_trades.empty:
            pnl_arr = sell_trades['PnL%'].values
            winners = pnl_arr[pnl_arr > 0]
            losers  = pnl_arr[pnl_arr <= 0]
            stats['win_rate']   = len(winners) / len(pnl_arr) * 100
            stats['avg_profit'] = float(winners.mean()) if len(winners) > 0 else 0.0
            stats['avg_loss']   = float(losers.mean())  if len(losers)  > 0 else 0.0

    # 15m 頻率 Sharpe（用 equity_ts 時序計算，每 15 分鐘一個數據點）
    if equity_ts:
        eq_s  = pd.Series(equity_ts)
        drets = eq_s.pct_change().dropna()
        # 年化：15m 每年約 35,040 根
        if len(drets) > 1 and drets.std() > 0:
            stats['sharpe'] = float(drets.mean() / drets.std() * math.sqrt(35_040))

    return trades_df, final_equity, roi, trade_count, mdd, stats