"""돌파 전략 (BTC/USD).

20기간 최고가를 거래량 급증(20기간 평균의 vol_mult배)과 함께 돌파하면 롱.
같은 거래량 조건으로 최저가를 깨면 숏/롱 청산.
2xATR 트레일링 스톱은 risk/manager와 backtest에서 처리.
"""
from indicators import rolling_high, rolling_low, sma


def signal(df, params, position=None):
    lb = int(params.get("lookback", 20))
    vol_mult = float(params.get("vol_mult", 1.5))
    if len(df) < lb + 1:
        return "hold"

    hi = rolling_high(df["high"], lb).iloc[-1]
    lo = rolling_low(df["low"], lb).iloc[-1]
    avg_vol = sma(df["volume"], lb).iloc[-1]
    price = df["close"].iloc[-1]
    vol = df["volume"].iloc[-1]
    if hi != hi or lo != lo or avg_vol != avg_vol:  # NaN 방어
        return "hold"

    vol_ok = vol >= vol_mult * avg_vol

    if position == "long":
        # 최저가 이탈 시 청산
        return "exit" if price < lo else "hold"
    if position == "short":
        return "exit" if price > hi else "hold"

    if price > hi and vol_ok:
        return "enter_long"
    if price < lo and vol_ok:
        return "enter_short"
    return "hold"
