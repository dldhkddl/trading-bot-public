import datetime as dt
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import dashboard
from tests.test_dashboard_overlay_status import dashboard_data


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(dashboard, "OPS_SESSION_TOKEN", "test-session-token")
    httpd = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, monkeypatch
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _post(server, path, payload, token="test-session-token", origin=None):
    port = server.server_port
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Ops-Token": token,
            "Origin": origin or f"http://127.0.0.1:{port}",
        },
    )
    with urlopen(request) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_controls_are_only_embedded_for_live_local_server():
    data = dashboard_data(None)
    live = dashboard.render_html(data, controls_enabled=True,
                                 ops_token="secret-value")
    static = dashboard.render_html(data, controls_enabled=False,
                                   ops_token="secret-value")
    assert "신규매수 잠금" in live and "전체정지" in live
    assert "자동주문 재개" in live and "미체결 주문 취소" in live
    assert "secret-value" in live
    assert "secret-value" not in static
    assert "수동 매수" not in live and "수동 매도" not in live


def test_post_rejects_missing_token_without_running_action(server):
    httpd, monkeypatch = server
    called = []
    monkeypatch.setattr(dashboard, "_perform_operation",
                        lambda *args: called.append(args))
    with pytest.raises(HTTPError) as caught:
        _post(httpd, "/api/operations/pause", {"confirmation": "전체정지"},
              token="wrong")
    assert caught.value.code == 403
    assert called == []


def test_post_accepts_local_origin_token_and_json(server):
    httpd, monkeypatch = server
    monkeypatch.setattr(
        dashboard,
        "_perform_operation",
        lambda path, payload: (200, {"ok": True, "path": path}),
    )
    status, body = _post(
        httpd,
        "/api/operations/buy-lock",
        {"confirmation": "신규매수 잠금"},
    )
    assert status == 200 and body["ok"] is True


def test_post_rejects_hostile_origin_even_with_valid_token(server):
    httpd, monkeypatch = server
    called = []
    monkeypatch.setattr(dashboard, "_perform_operation",
                        lambda *args: called.append(args))
    with pytest.raises(HTTPError) as caught:
        _post(httpd, "/api/operations/pause", {"confirmation": "전체정지"},
              origin="http://attacker.example")
    assert caught.value.code == 403
    assert called == []


def test_get_rejects_hostile_host_header(server):
    httpd, _ = server
    request = Request(
        f"http://127.0.0.1:{httpd.server_port}/",
        headers={"Host": "attacker.example"},
    )
    with pytest.raises(HTTPError) as caught:
        urlopen(request)
    assert caught.value.code == 403


def test_mode_change_cannot_race_active_liquidation_lock(
        server, tmp_path, monkeypatch):
    import ops_mode
    import order_lock

    monkeypatch.setattr(order_lock, "LOCK_FILE", str(tmp_path / "orders.lock"))
    monkeypatch.setattr(ops_mode, "load_mode", lambda: {
        "mode": ops_mode.PAUSED_ALL, "degraded": False,
    })
    with order_lock.order_lock("emergency-liquidate"):
        status, body = dashboard._perform_operation(
            "/api/operations/buy-lock",
            {"confirmation": "신규매수 잠금"},
        )
    assert status == 409
    assert body["ok"] is False


def test_control_post_is_served_while_account_page_get_is_blocked(monkeypatch):
    monkeypatch.setattr(dashboard, "OPS_SESSION_TOKEN", "test-session-token")
    entered = threading.Event()
    release = threading.Event()

    def blocked_collect():
        entered.set()
        release.wait(timeout=5)
        return dashboard_data(None)

    monkeypatch.setattr(dashboard, "collect", blocked_collect)
    monkeypatch.setattr(
        dashboard,
        "_perform_operation",
        lambda path, payload: (200, {"ok": True}),
    )
    httpd = dashboard.DashboardServer(("127.0.0.1", 0), dashboard.Handler)
    serving = threading.Thread(target=httpd.serve_forever, daemon=True)
    serving.start()
    slow_get = threading.Thread(
        target=lambda: urlopen(
            f"http://127.0.0.1:{httpd.server_port}/", timeout=6
        ).read(),
        daemon=True,
    )
    slow_get.start()
    try:
        assert entered.wait(timeout=2)
        started = time.monotonic()
        status, body = _post(
            httpd,
            "/api/operations/resume",
            {"confirmation": "자동주문 재개"},
        )
        assert status == 200 and body["ok"]
        assert time.monotonic() - started < 1.5
    finally:
        release.set()
        slow_get.join(timeout=2)
        httpd.shutdown()
        httpd.server_close()
        serving.join(timeout=2)


def test_resume_guard_fails_closed_on_missing_or_stale_assets():
    now = dt.datetime(2026, 8, 6, 6, 0, tzinfo=dt.timezone.utc)
    ok, _ = dashboard._resume_guard({}, now=now)
    assert not ok
    state = {
        "assets": {
            "BTC/USD": {
                "checked_at": "2026-08-06T05:00:00+00:00",
                "stale_after_sec": 1200,
            }
        },
        "failures": [],
    }
    ok, reason = dashboard._resume_guard(state, now=now)
    assert not ok and "오래" in reason


def test_resume_guard_accepts_fresh_error_free_watch():
    now = dt.datetime(2026, 8, 6, 6, 0, tzinfo=dt.timezone.utc)
    state = {
        "assets": {
            "BTC/USD": {
                "checked_at": "2026-08-06T05:50:00+00:00",
                "stale_after_sec": 1200,
            }
        },
        "failures": [],
    }
    assert dashboard._resume_guard(state, now=now)[0]


def test_resume_operation_refuses_degraded_mode_even_with_fresh_watch(monkeypatch):
    monkeypatch.setattr(dashboard, "_load_watch", lambda: {
        "assets": {"BTC/USD": {
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "stale_after_sec": 1200,
        }},
        "failures": [],
    })
    import ops_mode
    monkeypatch.setattr(ops_mode, "load_mode", lambda: {
        "mode": ops_mode.PAUSED_ALL,
        "degraded": True,
        "reason": "mode file missing",
    })
    status, body = dashboard._perform_operation(
        "/api/operations/resume", {"confirmation": "자동주문 재개"}
    )
    assert status == 409
    assert body["ok"] is False
