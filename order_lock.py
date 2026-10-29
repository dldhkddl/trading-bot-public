"""OS-released cross-process lock shared by every order-producing path."""
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import uuid


LOCK_FILE = "state/order_execution.lock"


class OrderBusyError(RuntimeError):
    pass


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _initialize_file(path):
    """Atomically create a non-empty persistent inode for byte-range locking."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"{}")
        handle.flush()
        os.fsync(handle.fileno())


def _try_lock(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - Windows is the production target
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise OrderBusyError("another order operation holds the OS lock") from exc


def _unlock(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _write_metadata(path, payload):
    tmp = path.with_name(f"{path.name}.{payload['token']}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


@contextmanager
def order_lock(operation, path=None, stale_after=None):
    """Acquire a kernel lock or raise; crashes release it without stale cleanup."""
    del stale_after  # retained for call compatibility; kernel owns crash recovery
    lock_path = Path(path if path is not None else LOCK_FILE)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    guard_path = lock_path.with_name(lock_path.name + ".guard")
    _initialize_file(guard_path)
    fd = os.open(guard_path, os.O_RDWR)
    locked = False
    owner = {
        "token": uuid.uuid4().hex,
        "pid": os.getpid(),
        "operation": str(operation),
        "started_at": _utcnow().isoformat(timespec="seconds"),
    }
    try:
        _try_lock(fd)
        locked = True
        _write_metadata(lock_path, owner)
        yield owner
    finally:
        if locked:
            released = dict(owner)
            released["released_at"] = _utcnow().isoformat(timespec="seconds")
            try:
                _write_metadata(lock_path, released)
            finally:
                _unlock(fd)
        os.close(fd)
