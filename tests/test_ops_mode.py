"""운영 모드 검증: 허용 규칙, fail-safe, 원자적 저장, 자동주문 게이트."""
import json
import pytest

import ops_mode as m


def test_allows_by_mode():
    assert m.allows("BUY", m.NORMAL) and m.allows("SELL(close)", m.NORMAL)
    # BUY_LOCKED: 매수만 차단, 방어 매도는 허용
    assert not m.allows("BUY", m.BUY_LOCKED)
    assert m.allows("SELL(close)", m.BUY_LOCKED)
    # PAUSED_ALL: 둘 다 차단
    assert not m.allows("BUY", m.PAUSED_ALL)
    assert not m.allows("SELL(close)", m.PAUSED_ALL)


def test_missing_file_fails_safe_to_paused(tmp_path):
    st = m.load_mode(tmp_path / "none.json")
    assert st["mode"] == m.PAUSED_ALL and st["degraded"]


def test_initialize_creates_normal_only_when_explicit(tmp_path):
    p = tmp_path / "ops.json"
    st = m.initialize_mode(p)
    assert st["mode"] == m.NORMAL
    assert m.load_mode(p)["mode"] == m.NORMAL


def test_initialize_does_not_overwrite_existing_mode(tmp_path):
    p = tmp_path / "ops.json"
    m.save_mode(m.BUY_LOCKED, path=p, audit_path=tmp_path / "audit.log")
    st = m.initialize_mode(p)
    assert st["mode"] == m.BUY_LOCKED


def test_corrupt_file_fails_safe_to_paused(tmp_path):
    p = tmp_path / "ops.json"
    p.write_text("{ broken json", encoding="utf-8")
    st = m.load_mode(p)
    assert st["mode"] == m.PAUSED_ALL and st["degraded"]


def test_unknown_mode_fails_safe_to_paused(tmp_path):
    p = tmp_path / "ops.json"
    p.write_text(json.dumps({"mode": "WHATEVER"}), encoding="utf-8")
    st = m.load_mode(p)
    assert st["mode"] == m.PAUSED_ALL and st["degraded"]


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "ops.json"
    m.save_mode(m.BUY_LOCKED, reason="테스트", path=p,
                audit_path=tmp_path / "audit.log")
    st = m.load_mode(p)
    assert st["mode"] == m.BUY_LOCKED and st["reason"] == "테스트"
    # 임시 파일이 남지 않아야 한다(원자적 교체)
    assert not (tmp_path / "ops.json.tmp").exists()


def test_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        m.save_mode("BOGUS", path=tmp_path / "ops.json",
                    audit_path=tmp_path / "audit.log")


def test_audit_failure_prevents_unlogged_mode_change(tmp_path, monkeypatch):
    p = tmp_path / "ops.json"
    audit = tmp_path / "audit.log"
    m.save_mode(m.BUY_LOCKED, path=p, audit_path=audit)

    def fail_audit(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(m, "_audit", fail_audit)
    with pytest.raises(OSError):
        m.save_mode(m.NORMAL, path=p, audit_path=audit)
    assert m.load_mode(p)["mode"] == m.BUY_LOCKED


def test_blocked_reason_codes():
    assert m.blocked_reason("BUY", m.BUY_LOCKED) == "MODE_BUY_LOCKED"
    assert m.blocked_reason("SELL(close)", m.PAUSED_ALL) == "MODE_PAUSED_ALL"
    assert m.blocked_reason("BUY", m.NORMAL) is None


def test_unknown_mode_is_never_allowed():
    assert not m.allows("BUY", "UNKNOWN")
    assert not m.allows("SELL", "UNKNOWN")
