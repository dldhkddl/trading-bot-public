"""시세 데이터. Alpaca에서 과거 봉을 받아오며, 키가 없거나 오프라인이면
재현 가능한 합성 데이터로 폴백해 백테스트를 돌려볼 수 있게 한다."""
import numpy as np
import pandas as pd


def synthetic_bars(symbol, periods=3000, freq="15min", seed=None):
    """테스트/오프라인용 합성 OHLCV. 자산별로 다른 시드를 줘 서로 다른 흐름을 만든다."""
    if seed is None:
        seed = abs(hash(symbol)) % (2**32)
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=periods, freq=freq, tz="UTC")
    # 완만한 추세 + 잡음
    drift = rng.normal(0.0002, 0.0002)
    rets = rng.normal(drift, 0.01, size=periods)
    close = 100 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.004, periods)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, periods)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = rng.integers(1_000, 100_000, periods).astype(float)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    return df


def get_bars(symbol, timeframe, start, end, env=None, allow_synthetic=True):
    """실데이터 우선, 실패 시 합성으로 폴백."""
    try:
        return _alpaca_bars(symbol, timeframe, start, end, env or {})
    except Exception as e:  # noqa: BLE001
        if not allow_synthetic:
            raise
        print(f"[data] Alpaca 데이터 실패({type(e).__name__}: {str(e)[:120]}) → 합성 폴백: {symbol}")
        freq = _tf_to_freq(timeframe)
        return synthetic_bars(symbol, freq=freq)


def _tf_to_freq(timeframe):
    return {
        "15Min": "15min", "1Hour": "1h", "4Hour": "4h", "1Day": "1D", "1Week": "1W",
    }.get(timeframe, "15min")


def _alpaca_bars(symbol, timeframe, start, end, env):
    # 지연 임포트 — alpaca-py 미설치 환경에서도 모듈 임포트가 가능하도록.
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    tf_map = {
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
        "1Week": TimeFrame(1, TimeFrameUnit.Week),
    }
    tf = tf_map[timeframe]
    key = env.get("ALPACA_API_KEY")
    sec = env.get("ALPACA_SECRET_KEY")

    if "/" in symbol:  # crypto
        client = CryptoHistoricalDataClient(key, sec)
        req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
        bars = client.get_crypto_bars(req)
    else:
        client = StockHistoricalDataClient(key, sec)
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf,
                               start=start, end=end, feed=DataFeed.IEX)
        bars = client.get_stock_bars(req)

    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)
    return df[["open", "high", "low", "close", "volume"]]
