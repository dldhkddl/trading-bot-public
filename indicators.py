"""공용 기술 지표. 모든 계산은 과거 데이터만 사용한다(미래 참조 없음)."""
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rolling_std(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).std(ddof=0)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    # 현재 봉을 제외한 직전 period개의 최고가 (돌파 판정에 사용)
    return series.shift(1).rolling(period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.shift(1).rolling(period).min()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range. df는 high/low/close 컬럼을 가진다."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()
