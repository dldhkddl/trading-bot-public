"""백테스트 누수 방지 검증: 신호가 난 봉의 종가가 아니라 '다음 봉 시가'로 체결."""
import pandas as pd
from backtest import fill_price


def _df():
    return pd.DataFrame({
        "open":  [10, 11, 12, 13],
        "high":  [10, 11, 12, 13],
        "low":   [10, 11, 12, 13],
        "close": [10.5, 11.5, 12.5, 13.5],
        "volume":[1, 1, 1, 1],
    })


def test_fill_uses_next_bar_open_not_current_close():
    df = _df()
    # 봉 인덱스 1에서 신호 → 체결은 봉 2의 시가(=12) 기준
    px = fill_price(df, 1, "long", slippage_bps=0)
    assert px == 12  # 다음 봉 시가
    assert px != df["close"].iloc[1]  # 현재 봉 종가(11.5)가 아니어야 함


def test_slippage_makes_buy_worse_and_sell_better_for_counterparty():
    df = _df()
    buy = fill_price(df, 1, "long", slippage_bps=100)   # 1%
    sell = fill_price(df, 1, "short", slippage_bps=100)
    assert buy > 12   # 살 때 더 비싸게
    assert sell < 12  # 팔 때 더 싸게


def test_last_bar_has_no_next_bar():
    df = _df()
    assert fill_price(df, len(df) - 1, "long", 0) is None
