from types import SimpleNamespace

import liquidate
import main


class FakeAccount:
    equity = "100000"


class FakeBrokerClient:
    def __init__(self, positions=()):
        self.positions = list(positions)

    def get_account(self):
        return FakeAccount()

    def get_all_positions(self):
        return self.positions


class FakeBroker:
    client = FakeBrokerClient()

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        return self.client


def test_liquidate_parser_defaults_to_dry_run():
    args = main.build_parser().parse_args(["liquidate"])
    assert args.execute is False
    assert not hasattr(args, "yes")


def test_dry_run_never_pauses_or_cancels(monkeypatch):
    monkeypatch.setattr(main.broker, "AlpacaBroker", FakeBroker)
    monkeypatch.setattr(liquidate, "prepare", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("dry-run must not mutate mode")
    ))
    rc = main.cmd_liquidate(SimpleNamespace(execute=False), {}, {})
    assert rc == 0


def test_actual_liquidation_aborts_when_cancel_verification_is_not_zero(
        monkeypatch):
    FakeBroker.client = FakeBrokerClient([SimpleNamespace(
        symbol="QQQ", qty="1", market_value="100"
    )])
    monkeypatch.setattr(main.broker, "AlpacaBroker", FakeBroker)
    monkeypatch.setattr("builtins.input", lambda prompt: "PAPER 전체청산")
    monkeypatch.setattr(liquidate, "prepare", lambda **kwargs: (True, None))
    monkeypatch.setattr(liquidate, "cancel_open_orders",
                        lambda client: (1, 1, None))
    called = []
    monkeypatch.setattr(liquidate, "close_all",
                        lambda *args, **kwargs: called.append(True))

    rc = main.cmd_liquidate(SimpleNamespace(execute=True), {}, {})
    assert rc == 2
    assert called == []


def test_actual_empty_account_still_pauses_and_checks_open_orders(monkeypatch):
    FakeBroker.client = FakeBrokerClient([])
    monkeypatch.setattr(main.broker, "AlpacaBroker", FakeBroker)
    monkeypatch.setattr("builtins.input", lambda prompt: "PAPER 전체청산")
    calls = []
    monkeypatch.setattr(liquidate, "prepare",
                        lambda **kwargs: (calls.append("pause") or True, None))
    monkeypatch.setattr(liquidate, "cancel_open_orders",
                        lambda client: (calls.append("cancel") or 0, 0, None))

    rc = main.cmd_liquidate(SimpleNamespace(execute=True), {}, {})
    assert rc == 0
    assert calls == ["pause", "cancel"]
