"""오버레이 (모의)운영 로직 — 추세필터로 각 코인을 '보유 vs 현금' 전환.

안전 원칙:
  - 이 모듈은 항상 모의(paper)로만 동작한다. 실계좌 경로는 열지 않는다.
  - dry-run 이면 주문을 계산만 하고 실제로는 넣지 않는다.
  - 결정은 전일까지의 종가로 판단(당일 미완성 봉에 의존하지 않음).
규칙: 가격 > N일 이동평균 → 보유(목표 비중까지 매수), 아래 → 현금(전량 청산).
"""
import csv
import datetime as dt
import hashlib
import os
from pathlib import Path

import ops_mode
from data import get_bars
from order_lock import OrderBusyError, order_lock
from overlay_status import AssetResult, CycleResult, save_cycle

LOG = "overlay_log.csv"
STATUS_FILE = "state/overlay_run_status.json"
FAILED_BROKER_STATES = {"rejected", "canceled", "expired", "suspended", "stopped"}
CONFIRMED_BROKER_STATES = {
    "accepted",
    "accepted_for_bidding",
    "new",
    "pending_new",
    "partially_filled",
    "filled",
    "pending_cancel",
    "pending_replace",
    "replaced",
}


def order_key(symbol, action, signal_time):
    """Stable, bounded Alpaca client order ID for one overlay decision."""
    raw = f"overlay|{symbol}|{action}|{signal_time}".encode("utf-8")
    return "ovl-" + hashlib.sha256(raw).hexdigest()[:24]


def _error_text(exc):
    return str(exc).replace("\n", " ")[:200]


def _broker_status(order):
    value = getattr(order, "status", "unknown")
    value = getattr(value, "value", value)
    return str(value).split(".")[-1].lower()


def _lookup_order(client, client_order_id):
    try:
        return client.get_order_by_client_id(client_order_id)
    except Exception as exc:  # Alpaca APIError and narrow test doubles
        if getattr(exc, "status_code", None) == 404:
            return None
        raise


def _result_from_order(symbol, action, client_order_id, order):
    if order is None:
        return AssetResult(
            asset=symbol,
            status="unconfirmed",
            action=action,
            client_order_id=client_order_id,
            error_code="SUBMIT_UNCONFIRMED",
            error="broker returned no order object",
        )
    status = _broker_status(order)
    observed_id = str(getattr(order, "client_order_id", "") or "")
    if observed_id != client_order_id or status not in (
        CONFIRMED_BROKER_STATES | FAILED_BROKER_STATES
    ):
        return AssetResult(
            asset=symbol,
            status="unconfirmed",
            action=action,
            client_order_id=client_order_id,
            broker_status=status,
            error_code="SUBMIT_UNCONFIRMED",
            error="broker response did not match the submitted order",
        )
    if status in FAILED_BROKER_STATES:
        return AssetResult(
            asset=symbol,
            status="failed",
            action=action,
            client_order_id=client_order_id,
            broker_status=status,
            error_code="ORDER_REJECTED",
            error=f"broker order status: {status}",
        )
    return AssetResult(
        asset=symbol,
        status="confirmed",
        action=action,
        client_order_id=client_order_id,
        broker_status=status,
    )


def execute_confirmed(
    client,
    symbol,
    action,
    allocation,
    signal_time,
    price=None,
    quantity=None,
):
    """Submit once and confirm by deterministic client order ID.

    Ambiguous submission errors are never retried. The order is looked up by
    the same ID and remains unconfirmed when the broker cannot prove receipt.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client_order_id = order_key(symbol, action, signal_time)
    try:
        existing = _lookup_order(client, client_order_id)
    except Exception as exc:  # noqa: BLE001
        return AssetResult(
            asset=symbol,
            status="failed",
            action=action,
            client_order_id=client_order_id,
            error_code="ORDER_LOOKUP_ERROR",
            error=_error_text(exc),
        )
    if existing is not None:
        return _result_from_order(symbol, action, client_order_id, existing)

    is_sell = action.upper().startswith("SELL")
    if is_sell:
        if quantity is None or float(quantity) <= 0:
            return AssetResult(
                asset=symbol,
                status="failed",
                action=action,
                client_order_id=client_order_id,
                error_code="INVALID_ORDER",
                error="sell quantity must be positive",
            )
        request = MarketOrderRequest(
            symbol=symbol,
            qty=abs(float(quantity)),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    else:
        if float(allocation) <= 0:
            return AssetResult(
                asset=symbol,
                status="failed",
                action=action,
                client_order_id=client_order_id,
                error_code="INVALID_ORDER",
                error="buy allocation must be positive",
            )
        request = MarketOrderRequest(
            symbol=symbol,
            notional=round(float(allocation), 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
            client_order_id=client_order_id,
        )

    try:
        order = client.submit_order(order_data=request)
    except Exception as exc:  # noqa: BLE001
        try:
            existing = _lookup_order(client, client_order_id)
        except Exception:  # The submission outcome remains ambiguous.
            existing = None
        if existing is not None:
            return _result_from_order(symbol, action, client_order_id, existing)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None and 400 <= int(status_code) < 500 and int(status_code) not in {408, 429}:
            return AssetResult(
                asset=symbol,
                status="failed",
                action=action,
                client_order_id=client_order_id,
                error_code="ORDER_SUBMIT_ERROR",
                error=_error_text(exc),
            )
        return AssetResult(
            asset=symbol,
            status="unconfirmed",
            action=action,
            client_order_id=client_order_id,
            error_code="SUBMIT_UNCONFIRMED",
            error=_error_text(exc),
        )
    result = _result_from_order(symbol, action, client_order_id, order)
    if result.status != "unconfirmed":
        return result
    try:
        existing = _lookup_order(client, client_order_id)
    except Exception:
        existing = None
    if existing is not None:
        return _result_from_order(symbol, action, client_order_id, existing)
    return result


def decide_asset(df, ma_period):
    """마지막 '완성된' 봉 기준으로 in/out 판단."""
    close = df["close"]
    ma_series = close.rolling(ma_period).mean()
    # 마지막 봉이 당일 미완성일 수 있어 직전 봉으로 판단
    price = float(close.iloc[-2])
    ma = float(ma_series.iloc[-2])
    if ma != ma:  # NaN
        return None, price, ma
    return ("in" if price > ma else "out"), price, ma


def _norm(sym):
    return sym.replace("/", "").upper()


def is_crypto(sym):
    return "/" in sym


def _fatal_cycle(started_at, message, equity=0.0):
    finished_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return CycleResult(
        started_at=started_at,
        finished_at=finished_at,
        equity=float(equity),
        assets=[],
        exit_code=2,
        fatal_error=message,
    )


def run_cycle(client, assets, ma_period, tf, dry_run, months, bars_loader, now=None,
              mode=None, mode_loader=None, lock_path=None):
    """Evaluate every asset and return a structured Paper overlay result."""
    end = now or dt.datetime.now(dt.timezone.utc)
    started_at = end.isoformat(timespec="seconds")
    try:
        account = client.get_account()
        equity = float(account.equity)
        buying_power = float(getattr(account, "buying_power", equity))
    except Exception as exc:  # noqa: BLE001
        return _fatal_cycle(started_at, f"account lookup failed: {_error_text(exc)}")
    if bool(getattr(account, "trading_blocked", False)):
        return _fatal_cycle(started_at, "account is trading blocked", equity)

    allocation = equity / max(len(assets), 1)
    try:
        positions = {_norm(p.symbol): p for p in client.get_all_positions()}
    except Exception as exc:  # noqa: BLE001
        return _fatal_cycle(started_at, f"position lookup failed: {_error_text(exc)}", equity)
    try:
        market_open = bool(client.get_clock().is_open)
    except Exception as exc:  # noqa: BLE001
        return _fatal_cycle(
            started_at,
            f"market clock lookup failed: {_error_text(exc)}",
            equity,
        )

    start = end - dt.timedelta(days=30 * months)
    results = []

    def current_mode():
        value = mode_loader() if mode_loader is not None else (
            mode if mode is not None else ops_mode.load_mode()
        )
        return value.get("mode") if isinstance(value, dict) else value

    def blocked_result(asset, action, active_mode, common):
        return AssetResult(
            asset,
            "blocked",
            action,
            error_code=ops_mode.blocked_reason(action, active_mode),
            error=f"운영 모드 {ops_mode.label(active_mode)} 로 주문 차단",
            **common,
        )

    for asset in assets:
        held_position = positions.get(_norm(asset))
        held = held_position is not None
        try:
            df = bars_loader(asset, tf, start, end)
            if len(df) < ma_period + 2:
                results.append(AssetResult(
                    asset=asset,
                    status="failed",
                    action="hold",
                    held=held,
                    allocation=round(allocation, 2),
                    error_code="INSUFFICIENT_BARS",
                    error=f"need at least {ma_period + 2} bars, got {len(df)}",
                ))
                continue
            signal, price, ma = decide_asset(df, ma_period)
            signal_time = str(df.index[-2])
        except Exception as exc:  # noqa: BLE001
            results.append(AssetResult(
                asset=asset,
                status="failed",
                action="hold",
                held=held,
                allocation=round(allocation, 2),
                error_code="DATA_ERROR",
                error=_error_text(exc),
            ))
            continue

        action = "hold"
        if signal == "in" and not held:
            action = "BUY"
        elif signal == "out" and held:
            action = "SELL"

        common = {
            "signal": signal,
            "price": round(price, 2),
            "ma": round(ma, 2),
            "held": held,
            "allocation": round(allocation, 2),
        }
        position_side = getattr(held_position, "side", None) if held_position else None
        position_side = getattr(position_side, "value", position_side)
        if held and str(position_side).lower() not in {"long", "position_side.long"}:
            results.append(AssetResult(
                asset,
                "failed",
                action,
                error_code="UNSUPPORTED_POSITION_SIDE",
                error=f"overlay supports long positions only, got {position_side}",
                **common,
            ))
            continue
        if action == "hold":
            results.append(AssetResult(asset, "no_action", action, **common))
            continue
        if not is_crypto(asset) and not market_open:
            results.append(AssetResult(asset, "deferred", action, **common))
            continue
        if dry_run:
            active_mode = current_mode()
            if not ops_mode.allows(action, active_mode):
                results.append(blocked_result(asset, action, active_mode, common))
            else:
                results.append(AssetResult(asset, "dry_run", action, **common))
            continue
        if action == "BUY" and allocation > buying_power:
            results.append(AssetResult(
                asset,
                "failed",
                action,
                error_code="INSUFFICIENT_BUYING_POWER",
                error=f"required {allocation:.2f}, available {buying_power:.2f}",
                **common,
            ))
            continue

        quantity = getattr(held_position, "qty", None) if held_position else None
        try:
            # Serialize every order path with emergency liquidation, then read
            # the mode inside the lock immediately before broker submission.
            with order_lock(f"overlay:{asset}:{action}", path=lock_path):
                active_mode = current_mode()
                if not ops_mode.allows(action, active_mode):
                    results.append(blocked_result(
                        asset, action, active_mode, common
                    ))
                    continue
                result = execute_confirmed(
                    client,
                    asset,
                    action,
                    allocation,
                    signal_time,
                    price=price,
                    quantity=quantity,
                )
        except OrderBusyError as exc:
            results.append(AssetResult(
                asset,
                "blocked",
                action,
                error_code="ORDER_EXECUTION_BUSY",
                error=_error_text(exc),
                **common,
            ))
            continue
        for key, value in common.items():
            setattr(result, key, value)
        results.append(result)
        if action == "BUY" and result.status == "confirmed":
            buying_power = max(0.0, buying_power - allocation)

    finished_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return CycleResult.from_assets(
        results,
        equity,
        started_at=started_at,
        finished_at=finished_at,
    )


LOG_FIELDS = [
    "started_at", "finished_at", "asset", "signal", "price", "ma", "held",
    "action", "allocation", "equity", "status", "client_order_id",
    "broker_status", "error_code", "error", "cycle_exit_code", "fatal_error",
]


def _migrate_legacy_log(path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames == LOG_FIELDS:
            return
        rows = list(reader)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        writer.writeheader()
        for row in rows:
            migrated = {field: row.get(field, "") for field in LOG_FIELDS}
            legacy_time = row.get("time", "")
            migrated["started_at"] = migrated["started_at"] or legacy_time
            migrated["finished_at"] = migrated["finished_at"] or legacy_time
            migrated["status"] = migrated["status"] or "legacy"
            writer.writerow(migrated)
    os.replace(tmp_path, path)


def write_cycle_log(cycle, path=LOG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        _migrate_legacy_log(path)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        for asset in cycle.assets:
            writer.writerow({
                "started_at": cycle.started_at,
                "finished_at": cycle.finished_at,
                "asset": asset.asset,
                "signal": asset.signal,
                "price": asset.price,
                "ma": asset.ma,
                "held": asset.held,
                "action": asset.action,
                "allocation": asset.allocation,
                "equity": round(cycle.equity, 2),
                "status": asset.status,
                "client_order_id": asset.client_order_id,
                "broker_status": asset.broker_status,
                "error_code": asset.error_code,
                "error": asset.error,
                "cycle_exit_code": cycle.exit_code,
                "fatal_error": cycle.fatal_error,
            })


def run_once(
    env,
    assets,
    ma_period,
    tf,
    dry_run=True,
    months=18,
    *,
    client=None,
    bars_loader=None,
    now=None,
    status_path=STATUS_FILE,
    log_path=LOG,
    mode=None,
):
    """Connect to Alpaca Paper, run one cycle, and persist its outcome.

    mode 가 None 이면 상태 파일에서 현재 운영 모드를 읽는다(fail-safe 적용).
    """
    started_at = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    if client is None:
        from broker import AlpacaBroker

        try:
            broker = AlpacaBroker(env, live_flag=False)
            if broker.decision.is_live:
                raise RuntimeError("overlay must remain in Paper mode")
            client = broker.connect()
        except Exception as exc:  # noqa: BLE001
            cycle = _fatal_cycle(started_at, f"broker connection failed: {_error_text(exc)}")
            save_cycle(cycle, status_path)
            return cycle

    if bars_loader is None:
        def bars_loader(symbol, timeframe, start, end):
            return get_bars(
                symbol,
                timeframe,
                start,
                end,
                env=env,
                allow_synthetic=False,
            )

    cycle = run_cycle(
        client,
        assets,
        ma_period,
        tf,
        dry_run,
        months,
        bars_loader,
        now=now,
        mode=mode,
    )
    try:
        save_cycle(cycle, status_path)
    except OSError as exc:
        cycle.exit_code = 2
        cycle.fatal_error = f"JSON persistence failed: {_error_text(exc)}"

    try:
        write_cycle_log(cycle, log_path)
    except OSError as exc:
        cycle.exit_code = 2
        message = f"CSV persistence failed: {_error_text(exc)}"
        cycle.fatal_error = (
            f"{cycle.fatal_error}; {message}" if cycle.fatal_error else message
        )
        try:
            save_cycle(cycle, status_path)
        except OSError as status_exc:
            cycle.fatal_error += f"; JSON persistence failed: {_error_text(status_exc)}"
    return cycle
