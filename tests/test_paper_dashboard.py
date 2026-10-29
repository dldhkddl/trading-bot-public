import csv
from types import SimpleNamespace

import paper_dashboard


def _account(equity="100250.50"):
    return SimpleNamespace(
        equity=equity,
        portfolio_value=equity,
        cash="62500.00",
        buying_power="125000.00",
        last_equity="100100.00",
        long_market_value="37750.50",
        short_market_value="0",
        currency="USD",
    )


def _position(symbol="SPY"):
    return SimpleNamespace(
        symbol=symbol,
        side=SimpleNamespace(value="long"),
        qty="16.103461602",
        avg_entry_price="776.23",
        current_price="771.11",
        market_value="12417.53",
        cost_basis="12500.00",
        unrealized_pl="-82.47",
        unrealized_plpc="-0.0065976",
    )


def test_fetch_paper_snapshot_uses_one_forced_paper_client(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")
    captured = {}

    class FakeClient:
        def get_account(self):
            return _account()

        def get_all_positions(self):
            return [_position("BTCUSD")]

    def factory(api_key, secret_key, *, paper):
        captured.update(api_key=api_key, secret_key=secret_key, paper=paper)
        return FakeClient()

    snapshot = paper_dashboard.fetch_paper_snapshot(
        client_factory=factory,
        now="2026-08-06T01:00:00+00:00",
    )

    assert captured == {
        "api_key": "paper-key",
        "secret_key": "paper-secret",
        "paper": True,
    }
    assert snapshot["ok"] is True
    assert snapshot["stale"] is False
    assert snapshot["account"]["equity"] == 100250.5
    assert snapshot["account"]["cash"] == 62500.0
    assert snapshot["positions"]["BTCUSD"] == {
        "symbol": "BTCUSD",
        "side": "long",
        "qty": 16.103461602,
        "avg_entry_price": 776.23,
        "current_price": 771.11,
        "market_value": 12417.53,
        "cost_basis": 12500.0,
        "unrealized_pl": -82.47,
        "unrealized_plpc": -0.0065976,
    }


def test_get_paper_snapshot_falls_back_to_cache_and_masks_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALPACA_API_KEY", "visible-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "visible-secret")
    cache = tmp_path / "paper_account_snapshot.json"

    class GoodClient:
        def get_account(self):
            return _account()

        def get_all_positions(self):
            return [_position()]

    first = paper_dashboard.get_paper_snapshot(
        client_factory=lambda *args, **kwargs: GoodClient(),
        cache_path=cache,
        now="2026-08-06T01:00:00+00:00",
    )

    def failing_factory(*args, **kwargs):
        raise RuntimeError("blocked visible-key visible-secret")

    stale = paper_dashboard.get_paper_snapshot(
        client_factory=failing_factory,
        cache_path=cache,
        now="2026-08-06T01:01:00+00:00",
    )

    assert first["stale"] is False
    assert stale["ok"] is False
    assert stale["stale"] is True
    assert stale["account"]["equity"] == 100250.5
    assert stale["updated_at"] == "2026-08-06T01:00:00+00:00"
    assert stale["error"] == "blocked *** ***"


def test_equity_history_respects_minimum_interval(tmp_path):
    path = tmp_path / "paper_equity_history.csv"
    snapshots = [
        {
            "ok": True,
            "stale": False,
            "updated_at": "2026-08-06T01:00:00+00:00",
            "account": {"equity": 100000.0, "cash": 60000.0, "buying_power": 120000.0},
        },
        {
            "ok": True,
            "stale": False,
            "updated_at": "2026-08-06T01:00:10+00:00",
            "account": {"equity": 100010.0, "cash": 60000.0, "buying_power": 120000.0},
        },
        {
            "ok": True,
            "stale": False,
            "updated_at": "2026-08-06T01:00:20+00:00",
            "account": {"equity": 100020.0, "cash": 60000.0, "buying_power": 120000.0},
        },
    ]

    for snapshot in snapshots:
        paper_dashboard.record_equity_snapshot(snapshot, path=path)

    history = paper_dashboard.load_equity_history(path)

    assert [row["timestamp"] for row in history] == [
        "2026-08-06T01:00:00+00:00",
        "2026-08-06T01:00:20+00:00",
    ]
    assert [row["equity"] for row in history] == [100000.0, 100020.0]


def test_history_seeds_from_unique_overlay_cycles(tmp_path):
    history_path = tmp_path / "paper_equity_history.csv"
    overlay_path = tmp_path / "overlay_log.csv"
    with overlay_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["finished_at", "asset", "equity"])
        writer.writeheader()
        writer.writerow({"finished_at": "2026-08-05T00:00:00+00:00", "asset": "SPY", "equity": "100000"})
        writer.writerow({"finished_at": "2026-08-05T00:00:00+00:00", "asset": "QQQ", "equity": "100000"})
        writer.writerow({"finished_at": "2026-08-05T01:00:00+00:00", "asset": "SPY", "equity": "100050"})

    history = paper_dashboard.load_equity_history(
        history_path,
        seed_path=overlay_path,
    )

    assert history == [
        {
            "timestamp": "2026-08-05T00:00:00+00:00",
            "equity": 100000.0,
            "cash": None,
            "buying_power": None,
        },
        {
            "timestamp": "2026-08-05T01:00:00+00:00",
            "equity": 100050.0,
            "cash": None,
            "buying_power": None,
        },
    ]


def test_history_normalizes_mixed_naive_and_aware_timestamps(tmp_path):
    history_path = tmp_path / "paper_equity_history.csv"
    overlay_path = tmp_path / "overlay_log.csv"
    with overlay_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["finished_at", "asset", "equity"])
        writer.writeheader()
        writer.writerow({"finished_at": "2026-08-05T00:00:00", "asset": "SPY", "equity": "100000"})
        writer.writerow({"finished_at": "2026-08-05T01:00:00+00:00", "asset": "SPY", "equity": "100050"})

    history = paper_dashboard.load_equity_history(
        history_path,
        seed_path=overlay_path,
    )

    assert [row["timestamp"] for row in history] == [
        "2026-08-05T00:00:00",
        "2026-08-05T01:00:00+00:00",
    ]
