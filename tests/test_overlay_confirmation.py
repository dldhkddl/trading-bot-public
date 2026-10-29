from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd
import pytest

import live_overlay as overlay_app
from live_overlay import (
    execute_confirmed,
    order_key,
    run_cycle,
    run_once,
    write_cycle_log,
)
from overlay_status import load_cycle


@pytest.fixture(autouse=True)
def normal_operating_mode(monkeypatch):
    """These order-outcome tests isolate broker behavior from local mode state."""
    monkeypatch.setattr(
        overlay_app.ops_mode,
        "load_mode",
        lambda *args, **kwargs: {
            "mode": overlay_app.ops_mode.NORMAL,
            "degraded": False,
        },
    )


class FakeNotFound(Exception):
    status_code = 404


@dataclass
class FakeOrder:
    status: str
    client_order_id: str = ""


class ExplicitReject(Exception):
    status_code = 422


class FakeClient:
    def __init__(
        self,
        *,
        existing=None,
        submitted=None,
        submit_error=None,
        existing_after_error=None,
    ):
        self.existing = existing
        self.submitted = submitted
        self.submit_error = submit_error
        self.existing_after_error = existing_after_error
        self.submit_calls = 0
        self.requests = []

    def get_order_by_client_id(self, client_order_id):
        if self.submit_calls and self.existing_after_error is not None:
            self.existing_after_error.client_order_id = client_order_id
            return self.existing_after_error
        if self.existing is not None:
            self.existing.client_order_id = client_order_id
            return self.existing
        raise FakeNotFound("order not found")

    def submit_order(self, order_data):
        self.submit_calls += 1
        self.requests.append(order_data)
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.client_order_id = order_data.client_order_id
        return self.submitted


class FakeTradingClient(FakeClient):
    def __init__(
        self,
        *,
        market_open=True,
        rejected_symbols=None,
        trading_blocked=False,
        buying_power=100_000,
        clock_error=None,
        positions=None,
    ):
        super().__init__()
        self.market_open = market_open
        self.clock_error = clock_error
        self.positions = list(positions or [])
        self.rejected_symbols = set(rejected_symbols or [])
        self.account = SimpleNamespace(
            equity="100000",
            buying_power=str(buying_power),
            trading_blocked=trading_blocked,
        )

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_clock(self):
        if self.clock_error is not None:
            raise self.clock_error
        return SimpleNamespace(is_open=self.market_open)

    def submit_order(self, order_data):
        self.submit_calls += 1
        self.requests.append(order_data)
        status = "rejected" if order_data.symbol in self.rejected_symbols else "accepted"
        return FakeOrder(status=status, client_order_id=order_data.client_order_id)


FIXED_NOW = pd.Timestamp("2026-08-05T12:00:00Z").to_pydatetime()


def in_signal_bars(*args, **kwargs):
    index = pd.date_range("2026-08-01", periods=5, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0, 4.0, 5.0],
            "high": [1.1, 2.1, 3.1, 4.1, 5.1],
            "low": [0.9, 1.9, 2.9, 3.9, 4.9],
            "close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "volume": [100] * 5,
        },
        index=index,
    )


def out_signal_bars(*args, **kwargs):
    frame = in_signal_bars()
    frame["close"] = [5.0, 4.0, 3.0, 2.0, 1.0]
    return frame


def test_order_key_is_stable_and_bounded():
    first = order_key("BTC/USD", "BUY", "2026-08-04T00:00:00+00:00")
    second = order_key("BTC/USD", "BUY", "2026-08-04T00:00:00+00:00")

    assert first == second
    assert first.startswith("ovl-")
    assert len(first) == 28


def test_existing_client_order_id_prevents_duplicate_submit():
    client = FakeClient(existing=FakeOrder(status="accepted"))

    result = execute_confirmed(
        client, "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "confirmed"
    assert result.broker_status == "accepted"
    assert client.submit_calls == 0


def test_submit_response_is_recorded_as_confirmed():
    client = FakeClient(submitted=FakeOrder(status="new"))

    result = execute_confirmed(
        client, "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "confirmed"
    assert result.client_order_id.startswith("ovl-")
    assert client.submit_calls == 1
    assert client.requests[0].client_order_id == result.client_order_id


def test_ambiguous_submit_rechecks_before_marking_unconfirmed():
    client = FakeClient(
        submitted=FakeOrder(status="new"),
        submit_error=TimeoutError("timeout"),
    )

    result = execute_confirmed(
        client, "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "unconfirmed"
    assert result.error_code == "SUBMIT_UNCONFIRMED"
    assert client.submit_calls == 1


def test_ambiguous_submit_found_by_id_is_confirmed_without_retry():
    client = FakeClient(
        submitted=FakeOrder(status="new"),
        submit_error=TimeoutError("timeout"),
        existing_after_error=FakeOrder(status="accepted"),
    )

    result = execute_confirmed(
        client, "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "confirmed"
    assert client.submit_calls == 1


def test_rejected_order_is_failed():
    client = FakeClient(submitted=FakeOrder(status="rejected"))

    result = execute_confirmed(
        client, "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "failed"
    assert result.error_code == "ORDER_REJECTED"


def test_null_submit_response_is_unconfirmed_not_success():
    class NullSubmitClient(FakeClient):
        def submit_order(self, order_data):
            self.submit_calls += 1
            return None

    result = execute_confirmed(
        NullSubmitClient(), "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "unconfirmed"
    assert result.error_code == "SUBMIT_UNCONFIRMED"


def test_explicit_four_xx_submission_error_is_failed_not_ambiguous():
    client = FakeClient(
        submitted=FakeOrder(status="new"),
        submit_error=ExplicitReject("insufficient buying power"),
    )

    result = execute_confirmed(
        client, "SPY", "BUY", 12_500, "2026-08-04", price=700
    )

    assert result.status == "failed"
    assert result.error_code == "ORDER_SUBMIT_ERROR"


def test_sell_submission_uses_position_quantity_and_order_id():
    client = FakeClient(submitted=FakeOrder(status="accepted"))

    result = execute_confirmed(
        client, "SPY", "SELL", 0, "2026-08-04", quantity=3.5
    )

    assert result.status == "confirmed"
    assert float(client.requests[0].qty) == 3.5
    assert client.requests[0].client_order_id == result.client_order_id


def test_dry_run_never_submits_orders():
    client = FakeTradingClient(market_open=True)

    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        True,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    assert cycle.assets[0].status == "dry_run"
    assert client.submit_calls == 0
    assert cycle.exit_code == 0


def test_closed_equity_market_is_deferred_not_failed():
    client = FakeTradingClient(market_open=False)

    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        False,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    assert cycle.assets[0].status == "deferred"
    assert cycle.exit_code == 0


def test_one_asset_failure_makes_cycle_fail_but_processes_remaining_assets():
    client = FakeTradingClient(market_open=True, rejected_symbols={"SPY"})

    cycle = run_cycle(
        client,
        ["SPY", "QQQ"],
        2,
        "1Day",
        False,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    assert [asset.asset for asset in cycle.assets] == ["SPY", "QQQ"]
    assert cycle.exit_code == 1
    assert cycle.counts["failed"] == 1
    assert cycle.counts["confirmed"] == 1


def test_trading_blocked_account_is_fatal():
    client = FakeTradingClient(trading_blocked=True)

    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        False,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    assert cycle.exit_code == 2
    assert cycle.fatal_error == "account is trading blocked"
    assert cycle.assets == []


def test_clock_lookup_failure_is_fatal_not_market_closed():
    client = FakeTradingClient(clock_error=TimeoutError("clock timeout"))

    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        False,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    assert cycle.exit_code == 2
    assert "market clock lookup failed" in cycle.fatal_error
    assert cycle.assets == []


def test_existing_short_position_is_not_sold_again():
    position = SimpleNamespace(symbol="SPY", qty="3", side="short")
    client = FakeTradingClient(positions=[position])

    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        False,
        18,
        bars_loader=out_signal_bars,
        now=FIXED_NOW,
    )

    assert cycle.exit_code == 1
    assert cycle.assets[0].error_code == "UNSUPPORTED_POSITION_SIDE"
    assert client.submit_calls == 0


def test_insufficient_buying_power_fails_before_submit():
    client = FakeTradingClient(market_open=True, buying_power=1_000)

    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        False,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    assert cycle.assets[0].status == "failed"
    assert cycle.assets[0].error_code == "INSUFFICIENT_BUYING_POWER"
    assert client.submit_calls == 0


def test_run_once_persists_cycle_and_expanded_csv(tmp_path):
    client = FakeTradingClient(market_open=True, rejected_symbols={"SPY"})
    status_path = tmp_path / "state" / "overlay_run_status.json"
    log_path = tmp_path / "overlay_log.csv"

    cycle = run_once(
        {},
        ["SPY"],
        2,
        "1Day",
        dry_run=False,
        months=18,
        client=client,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
        status_path=status_path,
        log_path=log_path,
    )

    csv_text = log_path.read_text(encoding="utf-8")
    header = csv_text.splitlines()[0]
    assert "status" in header
    assert "client_order_id" in header
    assert "broker_status" in header
    assert "error_code" in header
    assert "error" in header
    assert "failed" in csv_text
    assert load_cycle(status_path).to_dict() == cycle.to_dict()


def test_csv_failure_is_persisted_as_fatal_status(tmp_path, monkeypatch):
    client = FakeTradingClient(market_open=True)
    status_path = tmp_path / "state" / "overlay_run_status.json"

    def fail_log(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(overlay_app, "write_cycle_log", fail_log)

    cycle = run_once(
        {},
        ["SPY"],
        2,
        "1Day",
        dry_run=True,
        months=18,
        client=client,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
        status_path=status_path,
        log_path=tmp_path / "overlay_log.csv",
    )

    persisted = load_cycle(status_path)
    assert cycle.exit_code == 2
    assert "CSV persistence failed" in cycle.fatal_error
    assert persisted.exit_code == 2
    assert persisted.fatal_error == cycle.fatal_error


def test_write_cycle_log_appends_one_row_per_asset(tmp_path):
    client = FakeTradingClient(market_open=True)
    cycle = run_cycle(
        client,
        ["SPY", "QQQ"],
        2,
        "1Day",
        True,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )
    path = tmp_path / "overlay_log.csv"

    write_cycle_log(cycle, path)

    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_write_cycle_log_preserves_legacy_rows_while_migrating_schema(tmp_path):
    path = tmp_path / "overlay_log.csv"
    path.write_text(
        "time,asset,signal,price,ma,held,action,equity,dry_run\n"
        "2026-08-04T00:00:00,BTC/USD,out,60000,65000,False,hold,100000,False\n",
        encoding="utf-8",
    )
    client = FakeTradingClient(market_open=True)
    cycle = run_cycle(
        client,
        ["SPY"],
        2,
        "1Day",
        True,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    write_cycle_log(cycle, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "client_order_id" in lines[0]
    assert "BTC/USD" in lines[1]
    assert "legacy" in lines[1]


def test_log_schema_upgrade_preserves_existing_confirmation_fields(tmp_path):
    path = tmp_path / "overlay_log.csv"
    path.write_text(
        "started_at,finished_at,asset,signal,price,ma,held,action,allocation,equity,"
        "status,client_order_id,broker_status,error_code,error\n"
        "2026-08-04T00:00:00,2026-08-04T00:00:01,SPY,in,700,650,False,BUY,"
        "12500,100000,confirmed,ovl-existing,accepted,,\n",
        encoding="utf-8",
    )
    client = FakeTradingClient(market_open=True)
    cycle = run_cycle(
        client,
        ["QQQ"],
        2,
        "1Day",
        True,
        18,
        bars_loader=in_signal_bars,
        now=FIXED_NOW,
    )

    write_cycle_log(cycle, path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "confirmed" in lines[1]
    assert "ovl-existing" in lines[1]
    assert "accepted" in lines[1]
