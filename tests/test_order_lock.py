import json

import pytest

from order_lock import OrderBusyError, order_lock


def test_second_owner_cannot_enter_active_order_lock(tmp_path):
    path = tmp_path / "orders.lock"
    with order_lock("overlay", path=path):
        with pytest.raises(OrderBusyError):
            with order_lock("liquidate", path=path):
                pass
    assert path.exists()  # persistent file; the OS lock, not deletion, owns safety
    with order_lock("after-release", path=path):
        pass


def test_lock_records_operation_and_owner(tmp_path):
    path = tmp_path / "orders.lock"
    with order_lock("emergency-liquidate", path=path) as owner:
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["operation"] == "emergency-liquidate"
        assert stored["token"] == owner["token"]


def test_lock_file_records_release_without_becoming_busy(tmp_path):
    path = tmp_path / "orders.lock"
    with order_lock("overlay", path=path):
        pass
    released = json.loads(path.read_text(encoding="utf-8"))
    assert released["released_at"]
    with order_lock("next-owner", path=path):
        assert json.loads(path.read_text(encoding="utf-8"))["operation"] == "next-owner"
