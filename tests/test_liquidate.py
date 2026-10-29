"""긴급 청산 검증: 정지 선저장, 실패 시 미시작, unconfirmed 구분."""
import liquidate
import ops_mode
from overlay_status import AssetResult


class FakePos:
    def __init__(self, symbol, qty="10"):
        self.symbol = symbol
        self.qty = qty


class FakeClient:
    def __init__(self, positions=(), orders=(), leftover=None,
                 close_raises=None, reget_raises=False):
        self._positions = list(positions)
        self._orders = list(orders)
        self._leftover = leftover            # 청산 후 남는 심볼 집합
        self._close_raises = close_raises or set()
        self._reget_raises = reget_raises
        self.closed = []
        self.cancelled = False
        self._calls = 0

    def get_orders(self):
        return self._orders

    def cancel_orders(self):
        self.cancelled = True
        self._orders = []

    def get_all_positions(self):
        self._calls += 1
        if self._calls > 1:                  # 청산 후 재조회
            if self._reget_raises:
                raise RuntimeError("network down")
            if self._leftover is None:
                return []
            return [p for p in self._positions if p.symbol in self._leftover]
        return self._positions

    def close_position(self, symbol):
        if symbol in self._close_raises:
            raise RuntimeError("reject")
        self.closed.append(symbol)


def fake_executor(client, symbol, action, allocation, signal_time,
                  price=None, quantity=None):
    assert action == "SELL_EMERGENCY"
    if symbol in client._close_raises:
        return AssetResult(symbol, "failed", action, error="reject")
    client.closed.append(symbol)
    return AssetResult(
        symbol,
        "confirmed",
        action,
        client_order_id=f"liq-{symbol}-{signal_time}",
        broker_status="accepted",
    )


def test_prepare_saves_paused_all_first(tmp_path, monkeypatch):
    path = tmp_path / "ops.json"
    monkeypatch.setattr(ops_mode, "MODE_FILE", str(path))
    monkeypatch.setattr(ops_mode, "AUDIT_LOG", str(tmp_path / "audit.log"))
    ok, err = liquidate.prepare(path=path)
    assert ok and err is None
    assert ops_mode.load_mode(path)["mode"] == ops_mode.PAUSED_ALL


def test_prepare_failure_blocks_liquidation(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(ops_mode, "save_mode", boom)
    ok, err = liquidate.prepare()
    assert not ok and "OSError" in err      # 정지 저장 실패 → 청산 시작 금지


def test_close_all_marks_closed():
    c = FakeClient(positions=[FakePos("QQQ"), FakePos("SPY")])
    results, fatal = liquidate.close_all(c, operation_id="op-1",
                                         executor=fake_executor)
    assert fatal is None
    assert {r["status"] for r in results} == {"closed"}
    assert set(c.closed) == {"QQQ", "SPY"}


def test_leftover_position_is_unconfirmed_not_closed():
    # SPY 가 청산 후에도 남아 있으면 'closed' 로 속이지 않는다
    c = FakeClient(positions=[FakePos("QQQ"), FakePos("SPY")], leftover={"SPY"})
    results, _ = liquidate.close_all(c, operation_id="op-2",
                                     executor=fake_executor)
    s = liquidate.summarize(results)
    assert [r["symbol"] for r in s["unconfirmed"]] == ["SPY"]
    assert [r["symbol"] for r in s["closed"]] == ["QQQ"]
    assert not s["ok"]


def test_close_exception_is_failed():
    c = FakeClient(positions=[FakePos("QQQ")], close_raises={"QQQ"})
    results, _ = liquidate.close_all(c, operation_id="op-3",
                                     executor=fake_executor)
    s = liquidate.summarize(results)
    assert len(s["failed"]) == 1 and not s["ok"]


def test_reget_failure_makes_unconfirmed():
    c = FakeClient(positions=[FakePos("QQQ")], reget_raises=True)
    results, _ = liquidate.close_all(c, operation_id="op-4",
                                     executor=fake_executor)
    assert results[0]["status"] == "unconfirmed"   # 확인 못 하면 성공이라 하지 않음


def test_cancel_orders_reports_remaining():
    c = FakeClient(orders=["o1", "o2"])
    tried, remaining, err = liquidate.cancel_open_orders(c)
    assert tried == 2 and remaining == 0 and err is None and c.cancelled


def test_close_all_uses_same_operation_id_for_retry_safety():
    c = FakeClient(positions=[FakePos("QQQ"), FakePos("SPY")])
    seen = []

    def recorder(client, symbol, action, allocation, signal_time, **kwargs):
        seen.append((symbol, signal_time))
        client.closed.append(symbol)
        return AssetResult(symbol, "confirmed", action,
                           client_order_id=f"id-{symbol}", broker_status="accepted")

    liquidate.close_all(c, operation_id="fixed-operation", executor=recorder)
    assert seen == [("QQQ", "fixed-operation"), ("SPY", "fixed-operation")]
