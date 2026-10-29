"""10분 위험 감시 — 읽기 전용. 절대 주문하지 않는다.

방어선(200일 이동평균)은 **직전 마감 일봉까지**로 계산한다(오늘 미완성 봉 제외).
장중 신호만으로 매매하지 않으며, 실제 주문은 완성된 일봉과 자동전략이 결정한다.

분류:
  안정      절대거리 3% 초과
  관찰      절대거리 1% 초과 3% 이하
  임박      절대거리 1% 이하
  이탈      보유 자산의 현재가가 방어선 아래
  진입후보  미보유 자산의 현재가가 방어선 위
"""
import json
import os
from pathlib import Path
import datetime as dt

STATE_FILE = "state/watch_status.json"
EVENT_LOG = "state/watch_events.log"

STABLE, WATCH, IMMINENT, BREACHED, CANDIDATE = (
    "안정", "관찰", "임박", "이탈", "진입후보")

# 감시 주기(초)
INTERVAL_CRYPTO = 600          # 코인 24시간 10분
INTERVAL_ETF_OPEN = 600        # ETF 장중 10분
INTERVAL_ETF_CLOSED = 3600     # ETF 장외·주말·휴장 1시간


def _now():
    return dt.datetime.now(dt.timezone.utc)


def is_crypto(sym):
    return "/" in sym


def merge_targets(configured_assets, positions):
    """Monitor configured assets plus every currently held Paper position."""
    targets = list(configured_assets)
    normalized = {a.replace("/", "").upper() for a in targets}
    for position in positions:
        symbol = str(position.symbol)
        asset_class = str(getattr(position, "asset_class", "")).lower()
        if "crypto" in asset_class and "/" not in symbol and symbol.upper().endswith("USD"):
            symbol = f"{symbol[:-3]}/USD"
        key = symbol.replace("/", "").upper()
        if key not in normalized:
            targets.append(symbol)
            normalized.add(key)
    return targets


def defense_line(df, ma_period, incomplete_last=True):
    """Moving average from completed bars; drop the tail only when incomplete."""
    closed = df.iloc[:-1] if incomplete_last else df
    if len(closed) < ma_period:
        return None
    return float(closed["close"].rolling(ma_period).mean().iloc[-1])


def last_bar_is_incomplete(df, asset, market_open, now=None):
    """Return whether the last daily bar represents the currently forming day."""
    if df is None or len(df) == 0:
        return False
    if not is_crypto(asset) and not market_open:
        return False
    now = now or _now()
    try:
        stamp = df.index[-1]
        if hasattr(stamp, "to_pydatetime"):
            stamp = stamp.to_pydatetime()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp.astimezone(dt.timezone.utc).date() == now.date()
    except Exception:  # unknown index: fail-safe by excluding the tail
        return True


def distance_pct(price, ma):
    if not ma:
        return None
    return (price - ma) / ma * 100.0


def classify(price, ma, held):
    """감시 단계를 판정한다."""
    d = distance_pct(price, ma)
    if d is None:
        return None, None
    a = abs(d)
    if held and d < 0:
        return BREACHED, d
    if a <= 1.0:
        return IMMINENT, d
    if a <= 3.0:
        return WATCH, d
    if not held and d > 0:
        return CANDIDATE, d
    return STABLE, d


def describe(asset, stage, d, held, market_note=""):
    side = "위" if d >= 0 else "아래"
    base = f"{asset}가 200일선 {abs(d):.1f}% {side}"
    if stage == IMMINENT:
        tail = "오늘 종가에 따라 매도 신호 가능" if held else "종가 유지 시 매수 신호 가능"
        return f"임박 · {base} — {tail}"
    if stage == BREACHED:
        return f"이탈 · {base} — 장중 잠정 상태, 종가 확인 필요{market_note}"
    if stage == CANDIDATE:
        return f"진입후보 · {base} — 종가 확인 후 매수 검토"
    if stage == WATCH:
        return f"관찰 · {base}"
    return f"안정 · {base}"


def next_interval(assets, market_open):
    """다음 감시까지 권장 대기 시간(초). 코인이 있으면 항상 10분."""
    if any(is_crypto(a) for a in assets):
        return INTERVAL_CRYPTO
    return INTERVAL_ETF_OPEN if market_open else INTERVAL_ETF_CLOSED


def should_check(asset, market_open, last_checked, now=None):
    """자산별 주기 도달 여부. 장 마감은 오류가 아니라 간격만 늘린다."""
    now = now or _now()
    if last_checked is None:
        return True
    interval = (INTERVAL_CRYPTO if is_crypto(asset)
                else (INTERVAL_ETF_OPEN if market_open else INTERVAL_ETF_CLOSED))
    return (now - last_checked).total_seconds() >= interval


def stale_after(asset, market_open):
    """Grace beyond the intended interval before the dashboard marks stale."""
    if is_crypto(asset) or market_open:
        return 2 * INTERVAL_CRYPTO
    return INTERVAL_ETF_CLOSED + INTERVAL_ETF_OPEN


def parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def load_state(path=None):
    p = Path(path if path is not None else STATE_FILE)
    if not p.exists():
        return {"assets": {}, "last_success": None, "stages": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"assets": {}, "last_success": None, "stages": {},
                "corrupt": True}


def save_state(state, path=None):
    p = Path(path if path is not None else STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def record_event(asset, stage, message, prev_stages, path=None):
    """단계가 바뀔 때만 기록한다(같은 단계 반복 억제)."""
    if prev_stages.get(asset) == stage:
        return False
    prev_stages[asset] = stage
    p = Path(path if path is not None else EVENT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"{_now().isoformat(timespec='seconds')} {message}\n")
    return True


def record_health_event(asset, status, message, prev_health, path=None):
    """Log only health transitions, including failure and recovery."""
    if prev_health.get(asset) == status:
        return False
    prev_health[asset] = status
    p = Path(path if path is not None else EVENT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(
            f"{_now().isoformat(timespec='seconds')} "
            f"health asset={asset} status={status} message={message}\n"
        )
    return True
