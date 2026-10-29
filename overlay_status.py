"""Structured results and persistence for one Paper overlay cycle."""

from collections import Counter
from dataclasses import asdict, dataclass
import datetime as dt
import json
import os
from pathlib import Path


VALID_STATUSES = {
    "no_action",
    "blocked",
    "deferred",
    "dry_run",
    "confirmed",
    "failed",
    "unconfirmed",
}
FAILED_STATUSES = {"failed", "unconfirmed"}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class AssetResult:
    asset: str
    status: str
    action: str
    signal: str | None = None
    price: float | None = None
    ma: float | None = None
    held: bool | None = None
    allocation: float | None = None
    client_order_id: str | None = None
    broker_status: str | None = None
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self):
        if self.status not in VALID_STATUSES:
            raise ValueError(f"invalid overlay asset status: {self.status}")

    def to_dict(self):
        return asdict(self)


@dataclass
class CycleResult:
    started_at: str
    finished_at: str
    equity: float
    assets: list[AssetResult]
    exit_code: int
    fatal_error: str | None = None

    @classmethod
    def from_assets(
        cls,
        assets,
        equity,
        started_at=None,
        finished_at=None,
    ):
        assets = list(assets)
        failed = any(asset.status in FAILED_STATUSES for asset in assets)
        return cls(
            started_at=started_at or _now(),
            finished_at=finished_at or _now(),
            equity=float(equity),
            assets=assets,
            exit_code=1 if failed else 0,
        )

    @property
    def ok(self):
        return self.exit_code == 0

    @property
    def counts(self):
        return dict(sorted(Counter(asset.status for asset in self.assets).items()))

    def to_dict(self):
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "equity": self.equity,
            "assets": [asset.to_dict() for asset in self.assets],
            "exit_code": self.exit_code,
            "fatal_error": self.fatal_error,
        }

    @classmethod
    def from_dict(cls, value):
        try:
            assets = [AssetResult(**item) for item in value["assets"]]
            result = cls(
                started_at=str(value["started_at"]),
                finished_at=str(value["finished_at"]),
                equity=float(value["equity"]),
                assets=assets,
                exit_code=int(value["exit_code"]),
                fatal_error=value.get("fatal_error"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("overlay status is invalid") from exc
        if result.exit_code not in {0, 1, 2}:
            raise ValueError("overlay status is invalid")
        has_failed_asset = any(asset.status in FAILED_STATUSES for asset in result.assets)
        if result.exit_code == 0 and (has_failed_asset or result.fatal_error):
            raise ValueError("overlay status is invalid")
        if result.exit_code == 1 and (not has_failed_asset or result.fatal_error):
            raise ValueError("overlay status is invalid")
        if result.exit_code == 2 and not result.fatal_error:
            raise ValueError("overlay status is invalid")
        return result


def save_cycle(result, path="state/overlay_run_status.json"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(result.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_cycle(path="state/overlay_run_status.json"):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("overlay status is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("overlay status is invalid")
    return CycleResult.from_dict(value)
