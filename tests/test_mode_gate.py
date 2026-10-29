"""운영 모드가 실제 자동주문 사이클에서 주문을 막는지 검증."""
import datetime as dt
import pandas as pd
import pytest

import ops_mode
from live_overlay import run_cycle


class FakeAccount:
    equity = "100000"
    buying_power = "100000"
    trading_blocked = False


class FakeClock:
    is_open = True


class FakePosition:
    def __init__(self, symbol, qty="10"):
        self.symbol = symbol
        self.qty = qty
        self.side = "long"


class FakeClient:
    """주문 제출을 기록만 하는 가짜 브로커."""
    def __init__(self, positions=()):
        self.submitted = []
        self._positions = list(positions)
        self.pending_cid = ""

    def get_account(self):
        return FakeAccount()

    def get_all_positions(self):
        return self._positions

    def get_clock(self):
        return FakeClock()

    def get_order_by_client_id(self, cid):
        # 실제 Alpaca 는 주문이 없으면 404 를 던진다(조회 장애와 구분됨)
        err = Exception("order not found")
        err.status_code = 404
        raise err

    def submit_order(self, order_data=None, **kwargs):
        req = order_data if order_data is not None else kwargs.get("req")
        self.submitted.append(req)
        self.pending_cid = getattr(req, "client_order_id", "")
        class O:
            status = "accepted"
            client_order_id = getattr(req, "client_order_id", "")
            id = "x"
        return O()

    def close_position(self, symbol):
        self.submitted.append(("close", symbol))
        client = self
        class O:
            status = "accepted"
            client_order_id = client.pending_cid
            id = "y"
        return O()


def _bars(trend_up=True, n=260):
    """상승(가격>200MA) 또는 하락(가격<200MA) 시계열."""
    base = list(range(100, 100 + n)) if trend_up else list(range(100 + n, 100, -1))
    idx = pd.date_range("2024-01-01", periods=len(base), freq="1D", tz="UTC")
    return pd.DataFrame({"open": base, "high": base, "low": base,
                         "close": base, "volume": [1.0] * len(base)}, index=idx)


def _loader(trend_up):
    def load(asset, tf, start, end):
        return _bars(trend_up)
    return load


def _run(mode, held=False, trend_up=True):
    positions = [FakePosition("QQQ")] if held else []
    client = FakeClient(positions)
    cycle = run_cycle(client, ["QQQ"], 200, "1Day", dry_run=False, months=18,
                      bars_loader=_loader(trend_up), mode=mode)
    return client, cycle.assets[0]


def test_normal_allows_buy():
    client, r = _run(ops_mode.NORMAL, held=False, trend_up=True)
    assert r.action == "BUY" and r.status == "confirmed"
    assert len(client.submitted) == 1


def test_buy_locked_blocks_buy_without_submitting():
    client, r = _run(ops_mode.BUY_LOCKED, held=False, trend_up=True)
    assert r.action == "BUY"
    assert r.status == "blocked"
    assert r.error_code == "MODE_BUY_LOCKED"
    assert client.submitted == []          # 주문 API 호출 없음


def test_buy_locked_still_allows_defensive_sell():
    client, r = _run(ops_mode.BUY_LOCKED, held=True, trend_up=False)
    assert r.action == "SELL"
    assert r.status == "confirmed"          # 방어 매도는 통과
    assert len(client.submitted) == 1


def test_paused_all_blocks_sell_too():
    client, r = _run(ops_mode.PAUSED_ALL, held=True, trend_up=False)
    assert r.action == "SELL"
    assert r.status == "blocked"
    assert r.error_code == "MODE_PAUSED_ALL"
    assert client.submitted == []


def test_mode_is_reloaded_inside_lock_immediately_before_submit(tmp_path):
    client = FakeClient([])
    cycle = run_cycle(
        client,
        ["QQQ"],
        200,
        "1Day",
        dry_run=False,
        months=18,
        bars_loader=_loader(True),
        mode=ops_mode.NORMAL,
        mode_loader=lambda: ops_mode.PAUSED_ALL,
        lock_path=tmp_path / "orders.lock",
    )
    result = cycle.assets[0]
    assert result.action == "BUY"
    assert result.status == "blocked"
    assert result.error_code == "MODE_PAUSED_ALL"
    assert client.submitted == []


def test_blocked_is_not_counted_as_failure():
    _, r = _run(ops_mode.PAUSED_ALL, held=False, trend_up=True)
    from overlay_status import FAILED_STATUSES
    assert r.status not in FAILED_STATUSES   # 차단은 실패가 아니다
