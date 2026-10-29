from types import SimpleNamespace

import live_overlay
import main as app
from overlay_status import AssetResult, CycleResult
from overlay_status import load_cycle


def run_args(dry_run=False):
    return SimpleNamespace(
        timeframe="1Day",
        ma=2,
        dry_run=dry_run,
        months=18,
        us=False,
    )


def config():
    return {"overlay_assets": ["SPY"]}


def cycle_with(*assets, exit_code=None, fatal_error=None):
    cycle = CycleResult.from_assets(
        list(assets),
        equity=100_000,
        started_at="2026-08-05T00:00:00+00:00",
        finished_at="2026-08-05T00:00:01+00:00",
    )
    if exit_code is not None:
        cycle.exit_code = exit_code
    cycle.fatal_error = fatal_error
    return cycle


def test_cmd_run_returns_one_and_never_prints_success_on_partial_failure(
    monkeypatch, capsys
):
    cycle = cycle_with(
        AssetResult(
            asset="SPY",
            status="failed",
            action="BUY",
            error_code="ORDER_REJECTED",
            error="broker rejected order",
        ),
        AssetResult(asset="QQQ", status="confirmed", action="BUY"),
    )
    monkeypatch.setattr(live_overlay, "run_once", lambda *args, **kwargs: cycle)

    code = app.cmd_run(run_args(), config(), {})
    output = capsys.readouterr().out

    assert code == 1
    assert "실패 1건" in output
    assert "SPY [ORDER_REJECTED]" in output
    assert "주문을 제출했습니다" not in output


def test_cmd_run_returns_zero_only_for_success(monkeypatch, capsys):
    cycle = cycle_with(
        AssetResult(asset="SPY", status="confirmed", action="BUY"),
        AssetResult(asset="QQQ", status="deferred", action="BUY"),
    )
    monkeypatch.setattr(live_overlay, "run_once", lambda *args, **kwargs: cycle)

    code = app.cmd_run(run_args(), config(), {})
    output = capsys.readouterr().out

    assert code == 0
    assert "확인 1건" in output
    assert "보류 1건" in output


def test_cmd_run_returns_two_for_fatal_cycle(monkeypatch, capsys):
    cycle = cycle_with(
        exit_code=2,
        fatal_error="broker connection failed: unauthorized",
    )
    monkeypatch.setattr(live_overlay, "run_once", lambda *args, **kwargs: cycle)

    code = app.cmd_run(run_args(), config(), {})
    output = capsys.readouterr().out

    assert code == 2
    assert "치명적 실패" in output
    assert "broker connection failed" in output
    assert "주문을 제출했습니다" not in output


def test_main_propagates_run_exit_code(monkeypatch):
    monkeypatch.setattr(app, "load_config", lambda: config())
    monkeypatch.setattr(app, "_env", lambda: {})
    monkeypatch.setattr(app, "cmd_run", lambda *args, **kwargs: 2)

    assert app.main(["run", "--dry-run"]) == 2


def test_run_configuration_error_returns_two_and_persists_fatal_status(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    def fail_config():
        raise ValueError("invalid yaml")

    monkeypatch.setattr(app, "load_config", fail_config)

    code = app.main(["run", "--dry-run"])
    persisted = load_cycle(tmp_path / "state" / "overlay_run_status.json")

    assert code == 2
    assert persisted.exit_code == 2
    assert "configuration failed" in persisted.fatal_error
    assert "치명적 실패" in capsys.readouterr().out
