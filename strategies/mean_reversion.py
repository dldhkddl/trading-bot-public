"""평균 회귀 전략 (SPY, QQQ).

가격이 이동평균 대비 entry_std 표준편차 아래면 롱, 위면 숏.
이동평균으로 돌아오면 청산.
반환: 'enter_long' | 'enter_short' | 'exit' | 'hold'
position: 현재 포지션 side ('long'|'short'|None)
"""
from indicators import sma, rolling_std


def signal(df, params, position=None):
    period = int(params.get("sma_period", 20))
    k = float(params.get("entry_std", 1.5))
    if len(df) < period + 1:
        return "hold"

    mid = sma(df["close"], period)
    sd = rolling_std(df["close"], period)
    price = df["close"].iloc[-1]
    m = mid.iloc[-1]
    s = sd.iloc[-1]
    if s == 0 or s != s:  # 0 또는 NaN
        return "hold"

    upper = m + k * s
    lower = m - k * s

    if position == "long":
        # 평균으로 복귀하면 청산
        return "exit" if price >= m else "hold"
    if position == "short":
        return "exit" if price <= m else "hold"

    # 무포지션: 밴드 바깥이면 진입
    if price < lower:
        return "enter_long"
    if price > upper:
        return "enter_short"
    return "hold"
