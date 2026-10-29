import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def isolate_ops_state(tmp_path, monkeypatch):
    """테스트가 실제 운영 상태 파일(운영 모드·감시 상태)을 읽거나 건드리지 않게 격리한다.

    이게 없으면 로컬에 남은 PAUSED_ALL 같은 상태가 다른 테스트 결과를 바꿔버린다.
    """
    import ops_mode
    import watch

    monkeypatch.setattr(ops_mode, "MODE_FILE", str(tmp_path / "ops_mode.json"))
    monkeypatch.setattr(ops_mode, "AUDIT_LOG", str(tmp_path / "ops_audit.log"))
    monkeypatch.setattr(watch, "STATE_FILE", str(tmp_path / "watch_status.json"))
    monkeypatch.setattr(watch, "EVENT_LOG", str(tmp_path / "watch_events.log"))
    yield
