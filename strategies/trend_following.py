"""추세 추종 전략 (GLD, USO — ETF 대리 자산).

50 EMA가 200 EMA를 위로 뚫으면 롱, 아래로 뚫으면 청산/숏.
3xATR 트레일링 스톱은 risk/manager와 backtest에서 처리.
"""
from indicators import ema


def signal(df, params, position=None):
    fast_p = int(params.get("fast_ema", 50))
    slow_p = int(params.get("slow_ema", 200))
    if len(df) < slow_p + 1:
        return "hold"

    fast = ema(df["close"], fast_p)
    slow = ema(df["close"], slow_p)
    f_now, f_prev = fast.iloc[-1], fast.iloc[-2]
    s_now, s_prev = slow.iloc[-1], slow.iloc[-2]

    cross_up = f_prev <= s_prev and f_now > s_now
    cross_down = f_prev >= s_prev and f_now < s_now

    if position == "long":
        return "exit" if cross_down else "hold"
    if position == "short":
        return "exit" if cross_up else "hold"

    if cross_up:
        return "enter_long"
    if cross_down:
        return "enter_short"
    return "hold"
