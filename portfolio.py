"""포트폴리오 상태 — 잔고 최고점, 포지션, STOPPED 상태를 파일에 보존한다.

프로그램을 껐다 켜도 기준이 초기화되지 않아야 정지 장치가 유효하다.
또한 처리한 신호 id를 저장해 재시작 시 같은 신호를 다시 보내지 않는다.
"""
import json
import os

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "portfolio_state.json")

_DEFAULT = {
    "high_water_mark": 0.0,
    "status": "RUNNING",       # RUNNING | STOPPED
    "positions": {},            # symbol -> {side, qty, entry, stop}
    "processed_signals": [],    # 중복 방지용 신호 키
}


def load(path=STATE_FILE):
    if not os.path.exists(path):
        return dict(_DEFAULT)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    merged = dict(_DEFAULT)
    merged.update(data)
    return merged


def save(state, path=STATE_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_high_water_mark(state, equity):
    if equity > state.get("high_water_mark", 0.0):
        state["high_water_mark"] = equity
    return state["high_water_mark"]


def mark_stopped(state):
    state["status"] = "STOPPED"
    return state


def resume(state):
    state["status"] = "RUNNING"
    return state


def is_stopped(state):
    return state.get("status") == "STOPPED"


def already_processed(state, signal_key):
    return signal_key in state.get("processed_signals", [])


def record_signal(state, signal_key, keep_last=500):
    sigs = state.setdefault("processed_signals", [])
    if signal_key not in sigs:
        sigs.append(signal_key)
        # 무한 증가 방지
        if len(sigs) > keep_last:
            del sigs[:-keep_last]
