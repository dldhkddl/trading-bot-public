import json

import pytest

from overlay_status import AssetResult, CycleResult, load_cycle, save_cycle


def test_failed_asset_makes_cycle_fail():
    cycle = CycleResult.from_assets(
        [
            AssetResult(asset="SPY", status="confirmed", action="BUY"),
            AssetResult(
                asset="QQQ",
                status="failed",
                action="BUY",
                error_code="API_ERROR",
            ),
        ],
        equity=100_000.0,
        started_at="2026-08-05T00:00:00+00:00",
        finished_at="2026-08-05T00:00:01+00:00",
    )

    assert cycle.ok is False
    assert cycle.exit_code == 1
    assert cycle.counts == {"confirmed": 1, "failed": 1}


def test_normal_and_deferred_assets_keep_cycle_successful():
    cycle = CycleResult.from_assets(
        [
            AssetResult(asset="SPY", status="no_action", action="hold"),
            AssetResult(asset="QQQ", status="deferred", action="BUY"),
        ],
        equity=100_000.0,
        started_at="2026-08-05T00:00:00+00:00",
        finished_at="2026-08-05T00:00:01+00:00",
    )

    assert cycle.ok is True
    assert cycle.exit_code == 0
    assert cycle.counts == {"deferred": 1, "no_action": 1}


def test_cycle_status_round_trips_as_json(tmp_path):
    path = tmp_path / "state" / "overlay_run_status.json"
    cycle = CycleResult.from_assets(
        [
            AssetResult(
                asset="SPY",
                status="confirmed",
                action="BUY",
                client_order_id="ovl-abc",
                broker_status="accepted",
            )
        ],
        equity=100_000.0,
        started_at="2026-08-05T00:00:00+00:00",
        finished_at="2026-08-05T00:00:01+00:00",
    )

    save_cycle(cycle, path)
    loaded = load_cycle(path)

    assert loaded.to_dict() == cycle.to_dict()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_missing_status_returns_none(tmp_path):
    assert load_cycle(tmp_path / "missing.json") is None


def test_invalid_status_is_rejected(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"exit_code": 0}), encoding="utf-8")

    with pytest.raises(ValueError, match="overlay status is invalid"):
        load_cycle(path)


def test_success_exit_code_with_failed_asset_is_rejected(tmp_path):
    path = tmp_path / "contradictory.json"
    path.write_text(
        json.dumps(
            {
                "started_at": "2026-08-05T00:00:00+00:00",
                "finished_at": "2026-08-05T00:00:01+00:00",
                "equity": 100_000,
                "exit_code": 0,
                "fatal_error": None,
                "assets": [
                    {
                        "asset": "SPY",
                        "status": "failed",
                        "action": "BUY",
                        "error_code": "ORDER_REJECTED",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="overlay status is invalid"):
        load_cycle(path)
