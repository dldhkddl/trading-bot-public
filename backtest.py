"""백테스트 엔진.

핵심 원칙(누수 방지):
  - 신호는 봉 i의 종가까지의 데이터로 판단한다.
  - 체결은 '다음 봉(i+1)의 시가'로 이뤄진다. 같은 봉 종가 체결 금지.
  - 수수료와 슬리피지를 항상 반영하고 결과에 함께 출력한다.
"""
import numpy as np
import pandas as pd

from strategies import get as get_strategy
from risk import manager as risk


def fill_price(df, signal_bar_index, side, slippage_bps):
    """신호가 봉 i에서 났을 때 실제 체결가 = 봉 i+1의 시가에 슬리피지 반영.
    다음 봉이 없으면 None(체결 불가)."""
    nxt = signal_bar_index + 1
    if nxt >= len(df):
        return None
    open_next = float(df["open"].iloc[nxt])
    slip = slippage_bps / 10000.0
    if side == "long":
        return open_next * (1 + slip)   # 살 때 불리하게
    return open_next * (1 - slip)       # 팔 때 불리하게


def run_backtest(df, strategy_name, params, config, symbol="ASSET",
                 starting_equity=100_000.0):
    """단일 자산 백테스트. trades(list[dict])와 equity_curve(Series)를 반환.

    체결 규칙:
      - 진입/전략청산: 신호는 봉 i 종가로 판단, 체결은 봉 i+1 시가(미래참조 방지).
      - 손절: 매 봉의 고가/저가가 손절선에 닿으면 그 봉에서 즉시 체결(갭이면 시가).
      - 트레일링: 보유 중 ATR 기준으로 손절선을 유리한 방향으로만 이동.
    """
    strat = strategy_name if callable(strategy_name) else get_strategy(strategy_name)
    slippage_bps = config["costs"]["slippage_bps"]
    commission_bps = config["costs"]["commission_bps"]
    risk_pct = config["account"]["risk_per_trade"]
    atr_period = config["risk_indicators"]["atr_period"]
    slip = slippage_bps / 10000.0

    atr_mult = float(params.get("atr_mult", 2.0))

    equity = starting_equity
    position = None
    trades = []
    equity_points = []

    warmup = max(int(params.get("slow_ema", 0)),
                 int(params.get("sma_period", 0)),
                 int(params.get("lookback", 0)), atr_period) + 2

    for i in range(warmup, len(df)):
        row = df.iloc[i]
        window = df.iloc[: i + 1]
        atr_val = risk.current_atr(window, atr_period)

        # 1) 손절 체결 검사 (봉 i의 고저) — 보유 중일 때만
        if position is not None:
            stop = position["stop"]
            hit, fill = False, None
            if position["side"] == "long" and float(row["low"]) <= stop:
                raw = float(row["open"]) if float(row["open"]) < stop else stop
                fill = raw * (1 - slip)      # 매도라 불리하게
                hit = True
            elif position["side"] == "short" and float(row["high"]) >= stop:
                raw = float(row["open"]) if float(row["open"]) > stop else stop
                fill = raw * (1 + slip)      # 숏청산(매수)라 불리하게
                hit = True
            if hit:
                pnl = _close_pnl(position, fill, commission_bps)
                equity += pnl
                trades.append({"time": str(df.index[i]), "symbol": symbol,
                               "side": position["side"], "entry": round(position["entry"], 4),
                               "exit": round(fill, 4), "qty": round(position["qty"], 6),
                               "pnl": round(pnl, 2), "reason": "stop"})
                position = None

        # 2) 트레일링 스톱 갱신 (여전히 보유 중이면)
        if (position is not None and params.get("trailing", True)
                and atr_val == atr_val and atr_val > 0):
            position["stop"] = risk.update_trailing_stop(
                position["stop"], float(row["close"]), atr_val,
                position["side"], atr_mult)

        # 3) 전략 신호 → 다음 봉 시가 체결 (마지막 봉은 다음봉이 없어 스킵)
        if i < len(df) - 1:
            side_pos = position["side"] if position else None
            sig = strat(window, params, side_pos)

            if position is not None and sig == "exit":
                opp = "short" if position["side"] == "long" else "long"
                px = fill_price(df, i, opp, slippage_bps)
                if px is not None:
                    pnl = _close_pnl(position, px, commission_bps)
                    equity += pnl
                    r = _trade_row(df, i, symbol, position, px, pnl)
                    r["reason"] = "signal"
                    trades.append(r)
                    position = None

            elif position is None and sig in ("enter_long", "enter_short"):
                side = "long" if sig == "enter_long" else "short"
                entry = fill_price(df, i, side, slippage_bps)
                if entry is not None and atr_val == atr_val and atr_val > 0:
                    stop = risk.atr_stop(entry, atr_val, side, atr_mult)
                    qty = risk.position_size(equity, risk_pct, entry, stop)
                    if qty > 0:
                        position = {"side": side, "qty": qty, "entry": entry,
                                    "stop": stop, "entry_time": df.index[i + 1]}

        equity_points.append((df.index[i], equity))

    curve = pd.Series(dict(equity_points)) if equity_points else pd.Series(dtype=float)
    return trades, curve


def _close_pnl(position, exit_px, commission_bps):
    qty = position["qty"]
    entry = position["entry"]
    comm = (entry + exit_px) * qty * (commission_bps / 10000.0)
    if position["side"] == "long":
        return (exit_px - entry) * qty - comm
    return (entry - exit_px) * qty - comm


def _trade_row(df, i, symbol, position, exit_px, pnl):
    return {
        "time": str(df.index[i + 1]),
        "symbol": symbol,
        "side": position["side"],
        "entry": round(position["entry"], 4),
        "exit": round(exit_px, 4),
        "qty": round(position["qty"], 6),
        "pnl": round(pnl, 2),
    }


def metrics(trades, curve, starting_equity=100_000.0):
    if not trades:
        return {"trades": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "profit_factor": 0, "max_drawdown": 0, "sharpe": 0, "total_return": 0}
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = wins.sum()
    gross_loss = abs(losses.sum())
    if len(curve) > 1:
        rets = curve.pct_change().dropna()
        sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() else 0.0
        roll_max = curve.cummax()
        max_dd = ((roll_max - curve) / roll_max).max()
    else:
        sharpe, max_dd = 0.0, 0.0
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(pnls), 3),
        "avg_win": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.mean(), 2) if len(losses) else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "max_drawdown": round(float(max_dd), 3),
        "sharpe": round(float(sharpe), 2),
        "total_return": round(pnls.sum() / starting_equity, 3),
    }
