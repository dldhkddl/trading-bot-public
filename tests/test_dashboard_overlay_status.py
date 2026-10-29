from contextlib import redirect_stdout
import io
from types import SimpleNamespace

import pandas as pd

import dashboard as dashboard_app
from dashboard import _load_overlay, render_html
from overlay_status import AssetResult, CycleResult


def dashboard_data(overlay_run):
    return {
        "generated": "2026-08-05 12:00:00",
        "status": "RUNNING",
        "source": "overlay_log.csv (모의 운영)",
        "risk_pct": 0.01,
        "max_dd_stop": 0.10,
        "positions": {},
        "assets_cfg": {},
        "overlay": None,
        "overlay_run": overlay_run,
        "paper_positions": None,
        "has_trades": False,
        "n_trades": 0,
        "total_pnl": 0.0,
        "win_rate": 0.0,
        "equity_curve": [],
        "drawdown_curve": [],
        "per_asset": {},
        "equity": 100_000.0,
        "hwm": 100_000.0,
        "drawdown": 0.0,
        "best": None,
        "worst": None,
    }


def failed_run():
    return CycleResult.from_assets(
        [
            AssetResult(
                asset="SPY<script>",
                status="failed",
                action="BUY",
                error_code="ORDER_REJECTED",
                error="broker <rejected>",
            ),
            AssetResult(asset="QQQ", status="confirmed", action="BUY"),
        ],
        equity=100_000,
        started_at="2026-08-05T00:00:00+00:00",
        finished_at="2026-08-05T00:00:01+00:00",
    ).to_dict()


def test_dashboard_renders_failed_overlay_as_red_alert_and_escapes_values():
    html = render_html(dashboard_data(failed_run()), auto_refresh=0)

    assert "오버레이 실행 실패" in html
    assert "SPY&lt;script&gt;" in html
    assert "broker &lt;rejected&gt;" in html
    assert "status-danger" in html
    assert "최근 실행 성공" not in html


def test_dashboard_reports_missing_status_without_claiming_success():
    html = render_html(dashboard_data(None), auto_refresh=0)

    assert "실행 기록 없음" in html
    assert "최근 실행 성공" not in html


def test_dashboard_reports_status_file_read_failure():
    html = render_html(
        dashboard_data({"load_error": "overlay status is invalid"}),
        auto_refresh=0,
    )

    assert "상태 파일 읽기 실패" in html
    assert "status-danger" in html


def test_dashboard_makes_blocked_orders_visible_even_with_zero_exit_code():
    data = dashboard_data({
        "started_at": "2026-08-06T00:00:00+00:00",
        "finished_at": "2026-08-06T00:00:01+00:00",
        "exit_code": 0,
        "fatal_error": None,
        "assets": [{
            "asset": "QQQ", "status": "blocked", "action": "BUY",
            "error_code": "MODE_BUY_LOCKED", "error": "운영 모드로 차단",
        }],
    })
    html = render_html(data, auto_refresh=0)
    assert "주문 차단" in html
    assert "차단 1건" in html
    assert "최근 실행 성공" not in html


def test_dashboard_escapes_overlay_positions_and_script_data():
    data = dashboard_data(None)
    data["source"] = "<source>"
    data["positions"] = {
        "<img src=x>": {"side": "long<script>", "qty": "1", "entry": 1, "stop": 0}
    }
    data["overlay"] = {
        "time": "<time>",
        "assets": [
            {
                "asset": "SPY<script>",
                "signal": "in",
                "price": "<price>",
                "ma": "<ma>",
                "held": True,
                "action": "BUY<img>",
            }
        ],
        "invested": 1,
        "n": 1,
        "cash_pct": 0.0,
        "equity": 100_000,
        "dry_run": "False",
    }
    data["per_asset"] = {
        "</script><script>alert(1)</script>": {"n": 1, "pnl": 1, "win_rate": 1}
    }

    html = render_html(data, auto_refresh=0)

    assert "<source>" not in html
    assert "SPY<script>" not in html
    assert "<img src=x>" not in html
    assert "</script><script>alert(1)</script>" not in html
    assert "SPY&lt;script&gt;" in html
    assert "&lt;img src=x&gt;" in html
    assert "\\u003c/script>\\u003cscript>alert(1)\\u003c/script>" in html


def test_overlay_loader_accepts_new_started_at_schema(tmp_path, monkeypatch):
    pd.DataFrame(
        [
            {
                "started_at": "2026-08-05T00:00:00+00:00",
                "finished_at": "2026-08-05T00:00:01+00:00",
                "asset": "SPY",
                "signal": "in",
                "price": 700,
                "ma": 650,
                "held": False,
                "action": "BUY",
                "equity": 100_000,
                "status": "confirmed",
            }
        ]
    ).to_csv(tmp_path / "overlay_log.csv", index=False)
    monkeypatch.chdir(tmp_path)

    overlay = _load_overlay()

    assert overlay["time"] == "2026-08-05T00:00:01+00:00"
    assert overlay["assets"][0]["asset"] == "SPY"


def test_paper_position_loader_forces_paper_and_normalizes_positions(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")
    captured = {}

    class FakeClient:
        def get_all_positions(self):
            return [
                SimpleNamespace(
                    symbol="BTCUSD",
                    side="long",
                    qty="0.25",
                    avg_entry_price="61234.50",
                )
            ]

    def client_factory(api_key, secret_key, *, paper):
        captured.update(api_key=api_key, secret_key=secret_key, paper=paper)
        return FakeClient()

    snapshot = dashboard_app._load_paper_positions(client_factory=client_factory)

    assert captured == {
        "api_key": "paper-key",
        "secret_key": "paper-secret",
        "paper": True,
    }
    assert snapshot["error"] is None
    assert snapshot["positions"] == {
        "BTCUSD": {
            "side": "long",
            "qty": "0.25",
            "avg_entry_price": "61234.50",
        }
    }
    assert snapshot["updated_at"]


def test_reconcile_uses_current_paper_position_instead_of_preorder_log():
    overlay = {
        "time": "2026-08-05T13:45:17+00:00",
        "assets": [
            {
                "asset": "SPY",
                "signal": "in",
                "price": 776.23,
                "ma": 700,
                "held": False,
                "action": "BUY",
            }
        ],
        "invested": 0,
        "n": 1,
        "cash_pct": 1.0,
        "equity": 100_000,
        "dry_run": "False",
    }
    snapshot = {
        "positions": {
            "SPY": {
                "side": "long",
                "qty": "16.103461602",
                "avg_entry_price": "776.23",
            }
        },
        "updated_at": "2026-08-06 10:00:00",
        "error": None,
    }

    reconciled = dashboard_app._reconcile_overlay_positions(overlay, snapshot)

    assert reconciled["assets"][0]["held"] is True
    assert reconciled["assets"][0]["qty"] == "16.103461602"
    assert reconciled["assets"][0]["avg_entry_price"] == "776.23"
    assert reconciled["invested"] == 1
    assert reconciled["cash_pct"] == 0.0


def test_reconcile_marks_holdings_unknown_when_paper_lookup_fails():
    overlay = {
        "time": "2026-08-05T13:45:17+00:00",
        "assets": [{"asset": "SPY", "held": False}],
        "invested": 0,
        "n": 1,
        "cash_pct": 1.0,
    }
    snapshot = {
        "positions": {},
        "updated_at": None,
        "error": "network unavailable",
    }

    reconciled = dashboard_app._reconcile_overlay_positions(overlay, snapshot)

    assert reconciled["assets"][0]["held"] is None
    assert reconciled["invested"] is None
    assert reconciled["cash_pct"] is None


def test_dashboard_renders_current_paper_holding_details():
    data = dashboard_data(None)
    data["paper_positions"] = {
        "positions": {},
        "updated_at": "2026-08-06 10:00:00",
        "error": None,
    }
    data["overlay"] = {
        "time": "2026-08-05T13:45:17+00:00",
        "assets": [
            {
                "asset": "SPY",
                "signal": "in",
                "price": 776.23,
                "ma": 700,
                "held": True,
                "qty": "16.103461602",
                "avg_entry_price": "776.23",
                "action": "BUY",
            }
        ],
        "invested": 1,
        "n": 1,
        "cash_pct": 0.0,
        "equity": 100_000,
        "dry_run": "False",
    }

    html = render_html(data, auto_refresh=15)

    assert "Paper 계좌 동기화 완료" in html
    assert "16.103461602" in html
    assert "776.23" in html
    assert "<td>예</td>" in html
    assert "window.setInterval" in html
    assert "if (chartModal.hidden) window.location.reload()" in html
    assert 'http-equiv="refresh"' not in html


def test_dashboard_renders_visible_paper_lookup_failure():
    data = dashboard_data(None)
    data["paper_positions"] = {
        "positions": {},
        "updated_at": None,
        "error": "proxy <blocked>",
    }
    data["overlay"] = {
        "time": "2026-08-05T13:45:17+00:00",
        "assets": [
            {
                "asset": "SPY",
                "signal": "in",
                "price": 776.23,
                "ma": 700,
                "held": None,
                "qty": None,
                "avg_entry_price": None,
                "action": "BUY",
            }
        ],
        "invested": None,
        "n": 1,
        "cash_pct": None,
        "equity": 100_000,
        "dry_run": "False",
    }

    html = render_html(data, auto_refresh=15)

    assert "Paper 계좌 조회 실패" in html
    assert "proxy &lt;blocked&gt;" in html
    assert "확인 실패" in html
    assert "status-danger" in html


def _live_snapshot(stale=False):
    return {
        "ok": not stale,
        "stale": stale,
        "error": "proxy blocked" if stale else None,
        "updated_at": "2026-08-06T01:00:00+00:00",
        "account": {
            "equity": 100250.50,
            "portfolio_value": 100250.50,
            "cash": 62500.00,
            "buying_power": 125000.00,
            "last_equity": 100100.00,
            "long_market_value": 37750.50,
            "short_market_value": 0.0,
            "currency": "USD",
        },
        "positions": {
            "SPY": {
                "symbol": "SPY",
                "side": "long",
                "qty": 16.103461602,
                "avg_entry_price": 776.23,
                "current_price": 771.11,
                "market_value": 12417.53,
                "cost_basis": 12500.00,
                "unrealized_pl": -82.47,
                "unrealized_plpc": -0.0065976,
            },
            "QQQ": {
                "symbol": "QQQ",
                "side": "long",
                "qty": 17.168017473,
                "avg_entry_price": 728.097465,
                "current_price": 730.00,
                "market_value": 12532.65,
                "cost_basis": 12500.00,
                "unrealized_pl": 32.65,
                "unrealized_plpc": 0.002612,
            },
        },
    }


def test_paper_dashboard_model_uses_account_and_position_values():
    history = [
        {"timestamp": "2026-08-06T00:59:45+00:00", "equity": 100000.0},
    ]

    model = dashboard_app._paper_dashboard_data(_live_snapshot(), history)

    assert model["equity"] == 100250.5
    assert model["total_pnl"] == 250.5
    assert model["daily_pnl"] == 150.5
    assert model["cash"] == 62500.0
    assert model["buying_power"] == 125000.0
    assert model["n_positions"] == 2
    assert model["equity_curve"] == [100000.0, 100250.5]
    assert model["drawdown_curve"] == [0.0, 0.0]
    assert model["per_asset"]["SPY"]["pnl"] == -82.47
    assert model["per_asset"]["SPY"]["return_pct"] == -0.65976
    assert model["per_asset"]["QQQ"]["market_value"] == 12532.65


def test_collect_seeds_history_before_recording_current_snapshot(monkeypatch):
    events = []
    snapshot = _live_snapshot()

    monkeypatch.setattr(dashboard_app, "get_paper_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        dashboard_app,
        "load_equity_history",
        lambda *args, **kwargs: events.append("load") or [],
    )
    monkeypatch.setattr(
        dashboard_app,
        "record_equity_snapshot",
        lambda *args, **kwargs: events.append("record"),
    )
    monkeypatch.setattr(dashboard_app, "_load_config", lambda: {
        "account": {"max_drawdown_stop": 0.10}, "assets": {}
    })
    monkeypatch.setattr(dashboard_app, "_load_state", lambda: {"status": "RUNNING"})
    monkeypatch.setattr(dashboard_app, "_load_overlay", lambda: None)
    monkeypatch.setattr(dashboard_app, "_load_overlay_run", lambda: None)

    dashboard_app.collect()

    assert events == ["load", "record", "load"]


def test_dashboard_replaces_local_trade_widgets_with_live_paper_metrics():
    data = dashboard_data(None)
    history = [
        {"timestamp": "2026-08-06T00:59:45+00:00", "equity": 100000.0},
        {"timestamp": "2026-08-06T01:00:00+00:00", "equity": 100250.5},
    ]
    data.update(dashboard_app._paper_dashboard_data(_live_snapshot(), history))
    data["paper_snapshot"] = _live_snapshot()
    data["paper_positions"] = _live_snapshot()
    data["positions"] = _live_snapshot()["positions"]

    html = render_html(data, auto_refresh=15)

    assert "현재 자산" in html
    assert "누적손익" in html
    assert "당일손익" in html
    assert "현금" in html
    assert "매수 가능 금액" in html
    assert "100,250.50" in html
    assert "+250.50" in html
    assert "+150.50" in html
    assert "미실현손익" in html
    assert "포지션 수익률" in html
    assert "보유 비중" in html
    assert "12,417.53" in html
    assert "-82.47" in html
    assert "총 체결" not in html
    assert "승률" not in html
    assert "거래당 위험" not in html
    assert "베스트 거래" not in html


def test_dashboard_marks_cached_account_values_as_stale():
    data = dashboard_data(None)
    snapshot = _live_snapshot(stale=True)
    data.update(dashboard_app._paper_dashboard_data(snapshot, []))
    data["paper_snapshot"] = snapshot
    data["paper_positions"] = snapshot
    data["positions"] = snapshot["positions"]
    data["overlay"] = {
        "time": "2026-08-06T01:00:00+00:00",
        "assets": [{
            "asset": "SPY", "signal": "in", "price": 771.11, "ma": 700,
            "held": True, "qty": 16.103461602, "avg_entry_price": 776.23,
            "action": "hold",
        }],
        "invested": 1, "n": 1, "cash_pct": 0.0, "dry_run": "False",
    }

    html = render_html(data, auto_refresh=15)

    assert "Paper 계좌 조회 실패" in html
    assert "오래된 정보" in html
    assert "proxy blocked" in html
    assert "2026-08-06T01:00:00+00:00" in html


def test_dashboard_uses_compact_chart_frames_and_scrollable_tables():
    data = dashboard_data(None)
    snapshot = _live_snapshot()
    data.update(dashboard_app._paper_dashboard_data(snapshot, []))
    data["paper_snapshot"] = snapshot
    data["paper_positions"] = snapshot
    data["positions"] = snapshot["positions"]
    data["overlay"] = {
        "time": "2026-08-06T01:00:00+00:00",
        "assets": [{
            "asset": "SPY", "signal": "in", "price": 771.11, "ma": 700,
            "held": True, "qty": 16.103461602, "avg_entry_price": 776.23,
            "action": "hold",
        }],
        "invested": 1, "n": 1, "cash_pct": 0.0, "dry_run": "False",
    }

    html = render_html(data, auto_refresh=15)

    assert 'class="two chart-row"' in html
    assert html.count('class="chart-frame') == 5
    assert html.count('class="table-scroll"') == 2
    assert 'class="chart-frame chart-wide"' in html
    assert 'class="chart-frame chart-small"' in html


def test_dashboard_renders_clickable_assets_and_accessible_candle_dialog():
    data = dashboard_data(None)
    snapshot = _live_snapshot()
    data.update(dashboard_app._paper_dashboard_data(snapshot, []))
    data["paper_snapshot"] = snapshot
    data["paper_positions"] = snapshot
    data["positions"] = snapshot["positions"]
    data["overlay"] = {
        "time": "2026-08-06T01:00:00+00:00",
        "assets": [{
            "asset": "BTC/USD", "signal": "out", "price": 64000, "ma": 70000,
            "held": False, "qty": None, "avg_entry_price": None, "action": "hold",
        }],
        "invested": 0, "n": 1, "cash_pct": 1.0, "dry_run": "False",
    }

    html = render_html(data, auto_refresh=15)

    assert 'data-chart-symbol="BTC/USD"' in html
    assert 'data-chart-symbol="SPY"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'id="asset-candle-canvas"' in html
    for range_key in ("1D", "1W", "1M", "3M", "1Y"):
        assert f'data-chart-range="{range_key}"' in html
    assert "/api/chart?symbol=" in html
    assert "window.location.protocol === 'file:'" in html
    assert "200봉 이동평균" in html


def test_dashboard_once_completion_message_is_cp949_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard_app, "collect", lambda: dashboard_data(None))
    monkeypatch.setattr(dashboard_app, "render_html", lambda *args, **kwargs: "<html></html>")
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp949", errors="strict")

    with redirect_stdout(console):
        dashboard_app.main(["--once"])
    console.flush()

    assert (tmp_path / "dashboard.html").read_text(encoding="utf-8") == "<html></html>"
    assert "저장 완료" in raw.getvalue().decode("cp949")
