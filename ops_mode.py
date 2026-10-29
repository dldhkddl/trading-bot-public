"""운영 모드 — 자동주문을 안전한 방향으로 제한한다.

모드:
  NORMAL      : 자동 매수·매도 모두 허용
  BUY_LOCKED  : 신규 매수만 차단(방어 매도는 허용) — 기본 안전 모드
  PAUSED_ALL  : 매수·매도 모두 차단(방어 매도까지 막힘)

우선순위: PAUSED_ALL > BUY_LOCKED > NORMAL
fail-safe: 상태 파일을 읽을 수 없거나 값이 유효하지 않으면 PAUSED_ALL 로 간주한다.
"""
import json
import os
import uuid
from pathlib import Path
import datetime as dt

NORMAL = "NORMAL"
BUY_LOCKED = "BUY_LOCKED"
PAUSED_ALL = "PAUSED_ALL"
VALID_MODES = (NORMAL, BUY_LOCKED, PAUSED_ALL)

MODE_FILE = "state/ops_mode.json"
AUDIT_LOG = "state/ops_audit.log"

MODE_LABELS = {
    NORMAL: "정상",
    BUY_LOCKED: "신규매수 잠금",
    PAUSED_ALL: "전체정지",
}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def load_mode(path=None):
    """Read mode fail-closed; missing, corrupt, or invalid means PAUSED_ALL."""
    p = Path(path if path is not None else MODE_FILE)
    if not p.exists():
        return {
            "mode": PAUSED_ALL,
            "reason": "운영 모드 파일 없음 — 안전 정지",
            "degraded": True,
        }
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"mode": PAUSED_ALL, "reason": "상태 파일 손상 — 안전 정지",
                "degraded": True}
    mode = data.get("mode")
    if mode not in VALID_MODES:
        return {"mode": PAUSED_ALL, "reason": f"알 수 없는 모드({mode}) — 안전 정지",
                "degraded": True}
    data.setdefault("reason", "")
    data["degraded"] = False
    return data


def save_mode(mode, reason="", actor="user", path=None, audit_path=None):
    """모드를 원자적으로 저장한다. 실패하면 예외를 그대로 올린다."""
    if mode not in VALID_MODES:
        raise ValueError(f"허용되지 않은 모드: {mode}")
    p = Path(path if path is not None else MODE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    before = load_mode(p)
    request_id = uuid.uuid4().hex
    payload = {
        "mode": mode,
        "reason": reason,
        "actor": actor,
        "changed_at": _now().isoformat(timespec="seconds"),
        "request_id": request_id,
    }
    # Audit authorization is written before the state transition. If the audit
    # disk is unavailable, the mode must not silently change.
    _audit({
        "event": "mode_change_authorized",
        "request_id": request_id,
        "actor": actor,
        "before": before.get("mode"),
        "after": mode,
        "reason": reason,
    }, path=audit_path)
    tmp = p.with_name(f"{p.name}.{request_id}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    try:
        _audit({
            "event": "mode_change_applied",
            "request_id": request_id,
            "actor": actor,
            "before": before.get("mode"),
            "after": mode,
            "reason": reason,
        }, path=audit_path)
    except OSError as exc:
        # The pre-change record still makes the transition traceable. Surface
        # the completion-log problem without falsely reporting state failure.
        payload["audit_warning"] = str(exc)
    return payload


def initialize_mode(path=None, audit_path=None):
    """Explicit deployment initialization; never overwrite an existing file."""
    p = Path(path if path is not None else MODE_FILE)
    if p.exists():
        return load_mode(p)
    if audit_path is None and path is not None:
        audit_path = p.parent / "ops_audit.log"
    return save_mode(
        NORMAL,
        reason="explicit deployment initialization",
        actor="system",
        path=p,
        audit_path=audit_path,
    )


def _audit(event, path=None):
    p = Path(path if path is not None else AUDIT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = dict(event) if isinstance(event, dict) else {"event": str(event)}
    record["timestamp"] = _now().isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def audit_event(event, path=None):
    """Append and fsync a structured operational audit event."""
    _audit(event, path=path)


def allows(action, mode):
    """해당 행동이 현재 모드에서 허용되는지."""
    if mode not in VALID_MODES:
        return False
    if mode == PAUSED_ALL:
        return False
    if mode == BUY_LOCKED:
        return not str(action).upper().startswith("BUY")
    return True


def blocked_reason(action, mode):
    if allows(action, mode):
        return None
    if mode == PAUSED_ALL:
        return "MODE_PAUSED_ALL"
    return "MODE_BUY_LOCKED"


def label(mode):
    return MODE_LABELS.get(mode, mode)
