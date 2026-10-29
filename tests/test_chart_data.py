import datetime as dt

import pandas as pd
import pytest

import chart_data


def _bars(count=210):
    index = pd.date_range("2026-01-01", periods=count, freq="1D", tz="UTC")
    close = pd.Series(range(1, count + 1), index=index, dtype=float)
    return pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": close * 100,
    }, index=index)


def test_build_chart_payload_returns_ohlcv_ma200_and_position_lines():
    captured = {}

    def bars_loader(symbol, timeframe, start, end, env, allow_synthetic):
        captured.update(
            symbol=symbol,
            timeframe=timeframe,
            allow_synthetic=allow_synthetic,
        )
        return _bars()

    snapshot = {
        "positions": {
            "BTCUSD": {
                "avg_entry_price": 75.5,
                "current_price": 210.0,
            }
        }
    }

    payload = chart_data.build_chart_payload(
        "BTC/USD",
        "1Y",
        allowed_symbols={"BTC/USD", "SPY"},
        bars_loader=bars_loader,
        snapshot=snapshot,
        now=dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc),
        env={"ALPACA_API_KEY": "x", "ALPACA_SECRET_KEY": "y"},
    )

    assert captured == {
        "symbol": "BTC/USD",
        "timeframe": "1Day",
        "allow_synthetic": False,
    }
    assert payload["symbol"] == "BTC/USD"
    assert payload["range"] == "1Y"
    assert payload["timeframe"] == "1Day"
    assert len(payload["bars"]) == 210
    assert payload["bars"][0] == {
        "time": "2026-01-01T00:00:00+00:00",
        "open": 0.5,
        "high": 2.0,
        "low": 0.0,
        "close": 1.0,
        "volume": 100.0,
        "ma200": None,
    }
    assert payload["bars"][-1]["ma200"] == 110.5
    assert payload["avg_entry_price"] == 75.5
    assert payload["current_price"] == 210.0


@pytest.mark.parametrize(
    ("symbol", "range_key", "message"),
    [
        ("EVIL", "1M", "허용되지 않은 자산"),
        ("BTC/USD", "10Y", "지원하지 않는 기간"),
    ],
)
def test_build_chart_payload_rejects_unknown_symbol_or_range(symbol, range_key, message):
    with pytest.raises(ValueError, match=message):
        chart_data.build_chart_payload(
            symbol,
            range_key,
            allowed_symbols={"BTC/USD"},
            bars_loader=lambda *args, **kwargs: _bars(),
            snapshot={"positions": {}},
            env={},
        )
