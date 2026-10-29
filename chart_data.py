"""Validated read-only OHLCV payloads for dashboard asset charts."""

import datetime as dt
import os

from dotenv import load_dotenv

from data import get_bars


RANGE_CONFIG = {
    "1D": {"timeframe": "15Min", "lookback_days": 14, "display_days": 1, "fallback_bars": 96},
    "1W": {"timeframe": "1Hour", "lookback_days": 60, "display_days": 7, "fallback_bars": 168},
    "1M": {"timeframe": "4Hour", "lookback_days": 120, "display_days": 30, "fallback_bars": 240},
    "3M": {"timeframe": "1Day", "lookback_days": 320, "display_days": 90, "fallback_bars": 90},
    "1Y": {"timeframe": "1Day", "lookback_days": 600, "display_days": 365, "fallback_bars": 365},
}


def _norm_symbol(symbol):
    return str(symbol).replace("/", "").upper()


def _environment():
    load_dotenv()
    return {
        "ALPACA_API_KEY": os.getenv("ALPACA_API_KEY"),
        "ALPACA_SECRET_KEY": os.getenv("ALPACA_SECRET_KEY"),
    }


def build_chart_payload(
    symbol,
    range_key,
    *,
    allowed_symbols,
    bars_loader=get_bars,
    snapshot=None,
    now=None,
    env=None,
):
    """Return JSON-ready OHLCV and 200-bar moving-average data."""
    symbol = str(symbol).upper()
    allowed = {str(item).upper() for item in allowed_symbols}
    if symbol not in allowed:
        raise ValueError("허용되지 않은 자산")
    if range_key not in RANGE_CONFIG:
        raise ValueError("지원하지 않는 기간")

    config = RANGE_CONFIG[range_key]
    end = now or dt.datetime.now(dt.timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    start = end - dt.timedelta(days=config["lookback_days"])
    frame = bars_loader(
        symbol,
        config["timeframe"],
        start,
        end,
        env=env if env is not None else _environment(),
        allow_synthetic=False,
    ).sort_index()
    if frame.empty:
        raise ValueError("차트 데이터가 없습니다")

    frame = frame.copy()
    frame["ma200"] = frame["close"].rolling(200).mean()
    cutoff = end - dt.timedelta(days=config["display_days"])
    visible = frame[frame.index >= cutoff]
    if visible.empty:
        visible = frame.tail(config["fallback_bars"])

    bars = []
    for timestamp, row in visible.iterrows():
        ma_value = row["ma200"]
        bars.append({
            "time": timestamp.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "ma200": None if ma_value != ma_value else round(float(ma_value), 8),
        })

    position = (snapshot or {}).get("positions", {}).get(_norm_symbol(symbol), {})
    return {
        "symbol": symbol,
        "range": range_key,
        "timeframe": config["timeframe"],
        "bars": bars,
        "avg_entry_price": position.get("avg_entry_price"),
        "current_price": position.get("current_price"),
    }
