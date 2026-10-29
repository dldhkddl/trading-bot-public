"""긴급 전체청산 — 모드와 무관하게 실행하되, 시작과 동시에 정지 상태로 고정한다.

순서(설계 확정):
  1) PAUSED_ALL 을 **먼저** 저장한다. 실패하면 청산을 시작하지 않는다.
     (정지를 못 건 채 청산하면 자동주문과 충돌할 수 있다)
  2) 미체결 주문 전체 취소 후 재조회
  3) 보유 포지션 전체 청산
  4) 자산별 체결·실패·미확인 확인 (unconfirmed 는 강하게 표시)
  5) 성공 여부와 무관하게 PAUSED_ALL 유지
"""
import datetime as dt
import hashlib

import ops_mode
from live_overlay import execute_confirmed


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _err(exc):
    return f"{type(exc).__name__}: {str(exc)[:150]}"


def prepare(actor="cli", path=None):
    """청산 전 안전장치: PAUSED_ALL 선저장. 실패 시 (False, 사유)."""
    try:
        kwargs = {"reason": "긴급 전체청산 시작", "actor": actor}
        if path is not None:
            kwargs["path"] = path
        ops_mode.save_mode(ops_mode.PAUSED_ALL, **kwargs)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, _err(exc)


def cancel_open_orders(client):
    """미체결 주문 전체 취소 후 재조회. (취소시도수, 남은주문수, 오류)"""
    try:
        orders = list(client.get_orders())
    except Exception as exc:  # noqa: BLE001
        return 0, None, f"주문 조회 실패: {_err(exc)}"
    if not orders:
        return 0, 0, None
    try:
        client.cancel_orders()
    except Exception as exc:  # noqa: BLE001
        return len(orders), None, f"주문 취소 실패: {_err(exc)}"
    try:
        remaining = len(list(client.get_orders()))
    except Exception as exc:  # noqa: BLE001
        return len(orders), None, f"취소 후 재조회 실패: {_err(exc)}"
    return len(orders), remaining, None


def liquidation_key(positions):
    """Stable key while the same position snapshot remains outstanding."""
    raw = "|".join(sorted(
        f"{p.symbol}:{getattr(p, 'qty', '')}" for p in positions
    ))
    return "emergency-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def close_all(client, operation_id=None, executor=execute_confirmed):
    """Close positions with deterministic order IDs, then re-query holdings."""
    try:
        positions = list(client.get_all_positions())
    except Exception as exc:  # noqa: BLE001
        return [], f"포지션 조회 실패: {_err(exc)}"

    operation_id = operation_id or liquidation_key(positions)
    results = []
    for p in positions:
        sym = p.symbol
        qty = getattr(p, "qty", None)
        try:
            order_result = executor(
                client,
                sym,
                "SELL_EMERGENCY",
                0,
                operation_id,
                quantity=qty,
            )
            status = {
                "confirmed": "submitted",
                "failed": "failed",
                "unconfirmed": "unconfirmed",
            }.get(order_result.status, "unconfirmed")
            err = order_result.error or None
            client_order_id = order_result.client_order_id
        except Exception as exc:  # noqa: BLE001
            status, err, client_order_id = "failed", _err(exc), ""
        results.append({
            "symbol": sym,
            "qty": qty,
            "status": status,
            "error": err,
            "client_order_id": client_order_id,
        })

    # 재조회로 실제 청산 여부 확인
    try:
        left = {p.symbol for p in client.get_all_positions()}
    except Exception as exc:  # noqa: BLE001
        for r in results:
            if r["status"] == "submitted":
                r["status"] = "unconfirmed"
                r["error"] = f"청산 후 재조회 실패: {_err(exc)}"
        return results, None

    for r in results:
        if r["status"] != "submitted":
            continue
        r["status"] = "unconfirmed" if r["symbol"] in left else "closed"
        if r["status"] == "unconfirmed":
            r["error"] = "포지션이 남아 있음 — Alpaca 주문 내역에서 직접 확인 필요"
    return results, None


def summarize(results):
    closed = [r for r in results if r["status"] == "closed"]
    failed = [r for r in results if r["status"] == "failed"]
    unconfirmed = [r for r in results if r["status"] == "unconfirmed"]
    return {"closed": closed, "failed": failed, "unconfirmed": unconfirmed,
            "ok": not failed and not unconfirmed}
