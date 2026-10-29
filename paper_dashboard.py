"""Read-only Alpaca Paper account snapshots for the local dashboard."""

import csv
import datetime as dt
import json
import os
from pathlib import Path

from dotenv import load_dotenv


HISTORY_FIELDS = ["timestamp", "equity", "cash", "buying_power"]


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _as_float(value):
    if value in (None, ""):
        return None
    return float(value)


def _symbol(value):
    return str(value).replace("/", "").upper()


def _masked_error(exc, secrets):
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


def fetch_paper_snapshot(client_factory=None, now=None):
    """Fetch account and positions from one client forced to Paper mode."""
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    updated_at = now or _now_iso()
    if not api_key or not secret_key:
        return {
            "ok": False,
            "stale": False,
            "error": "Paper 계좌 키가 없습니다",
            "updated_at": None,
            "account": {},
            "positions": {},
        }
    if client_factory is None:
        from alpaca.trading.client import TradingClient
        client_factory = TradingClient
    try:
        client = client_factory(api_key, secret_key, paper=True)
        account = client.get_account()
        raw_positions = client.get_all_positions()
        account_data = {
            "equity": _as_float(getattr(account, "equity", None)),
            "portfolio_value": _as_float(getattr(account, "portfolio_value", None)),
            "cash": _as_float(getattr(account, "cash", None)),
            "buying_power": _as_float(getattr(account, "buying_power", None)),
            "last_equity": _as_float(getattr(account, "last_equity", None)),
            "long_market_value": _as_float(getattr(account, "long_market_value", None)),
            "short_market_value": _as_float(getattr(account, "short_market_value", None)),
            "currency": str(getattr(account, "currency", "USD")),
        }
        positions = {}
        for position in raw_positions:
            side = getattr(position, "side", None)
            side = getattr(side, "value", side)
            symbol = _symbol(getattr(position, "symbol", ""))
            positions[symbol] = {
                "symbol": symbol,
                "side": str(side),
                "qty": _as_float(getattr(position, "qty", None)),
                "avg_entry_price": _as_float(getattr(position, "avg_entry_price", None)),
                "current_price": _as_float(getattr(position, "current_price", None)),
                "market_value": _as_float(getattr(position, "market_value", None)),
                "cost_basis": _as_float(getattr(position, "cost_basis", None)),
                "unrealized_pl": _as_float(getattr(position, "unrealized_pl", None)),
                "unrealized_plpc": _as_float(getattr(position, "unrealized_plpc", None)),
            }
        return {
            "ok": True,
            "stale": False,
            "error": None,
            "updated_at": updated_at,
            "account": account_data,
            "positions": positions,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "stale": False,
            "error": _masked_error(exc, (api_key, secret_key)),
            "updated_at": None,
            "account": {},
            "positions": {},
        }


def _save_json_atomic(value, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _load_json(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def get_paper_snapshot(
    client_factory=None,
    cache_path="state/paper_account_snapshot.json",
    now=None,
):
    """Return a live snapshot, or an explicitly stale last-known snapshot."""
    live = fetch_paper_snapshot(client_factory=client_factory, now=now)
    if live["ok"]:
        _save_json_atomic(live, cache_path)
        return live
    cached = _load_json(cache_path)
    if cached and cached.get("account"):
        cached["ok"] = False
        cached["stale"] = True
        cached["error"] = live["error"]
        return cached
    return live


def _parse_timestamp(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _read_history(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows.append({
                    "timestamp": row["timestamp"],
                    "equity": float(row["equity"]),
                    "cash": _as_float(row.get("cash")),
                    "buying_power": _as_float(row.get("buying_power")),
                })
    except (OSError, KeyError, TypeError, ValueError):
        return []
    return rows


def _write_history(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _seed_overlay_history(path):
    path = Path(path)
    if not path.exists():
        return []
    by_time = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                timestamp = row.get("finished_at") or row.get("time")
                equity = row.get("equity")
                if timestamp and equity not in (None, ""):
                    by_time[timestamp] = {
                        "timestamp": timestamp,
                        "equity": float(equity),
                        "cash": None,
                        "buying_power": None,
                    }
    except (OSError, TypeError, ValueError):
        return []
    return [by_time[key] for key in sorted(by_time, key=_parse_timestamp)]


def load_equity_history(path="state/paper_equity_history.csv", seed_path=None):
    rows = _read_history(path)
    if not rows and seed_path:
        rows = _seed_overlay_history(seed_path)
        if rows:
            _write_history(rows, path)
    return sorted(rows, key=lambda row: _parse_timestamp(row["timestamp"]))


def record_equity_snapshot(
    snapshot,
    path="state/paper_equity_history.csv",
    min_interval_seconds=15,
    retention_days=30,
):
    """Persist successful live equity at a bounded cadence and retention."""
    if not snapshot.get("ok") or snapshot.get("stale"):
        return
    timestamp = snapshot.get("updated_at")
    equity = snapshot.get("account", {}).get("equity")
    if not timestamp or equity is None:
        return
    rows = _read_history(path)
    current_time = _parse_timestamp(timestamp)
    if rows:
        last_time = _parse_timestamp(rows[-1]["timestamp"])
        if (current_time - last_time).total_seconds() < min_interval_seconds:
            return
    cutoff = current_time - dt.timedelta(days=retention_days)
    rows = [row for row in rows if _parse_timestamp(row["timestamp"]) >= cutoff]
    account = snapshot["account"]
    rows.append({
        "timestamp": timestamp,
        "equity": float(equity),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
    })
    _write_history(rows, path)
