"""리스크 관리 — 봇에서 가장 먼저 만들고 마지막까지 지키는 부분.

핵심 규칙:
  1) 거래당 최대 위험 = 자기자본 x risk_pct (기본 1%)
  2) 수량은 손절 거리로 결정한다(손절을 먼저, 수량을 나중에).
  3) 상관 필터: SPY와 QQQ가 둘 다 롱이면 BTC/USD 신규 롱 금지.
  4) 하드 스톱: 모든 거래는 진입 시 손절이 정해진다.
  5) 최대 낙폭 정지: 고점 대비 max_dd 이상 하락하면 전량 정리 후 STOPPED.
"""
from indicators import atr as _atr


def position_size(equity, risk_pct, entry_price, stop_price):
    """위험 예산 / 손절까지의 거리 = 수량.
    손절 거리가 0 이하이면 주문을 건너뛰기 위해 0을 반환한다."""
    risk_budget = equity * risk_pct
    distance = abs(entry_price - stop_price)
    if distance <= 0:
        return 0.0
    return risk_budget / distance


def atr_stop(entry_price, atr_value, side, atr_mult):
    """ATR 기반 손절 가격. side: 'long' | 'short'."""
    if side == "long":
        return entry_price - atr_mult * atr_value
    return entry_price + atr_mult * atr_value


def current_atr(df, period=14):
    return _atr(df, period).iloc[-1]


def correlation_blocks_btc_long(open_positions, enabled=True):
    """SPY와 QQQ가 둘 다 롱이면 True(=BTC 신규 롱 차단)."""
    if not enabled:
        return False
    spy = open_positions.get("SPY", {}).get("side")
    qqq = open_positions.get("QQQ", {}).get("side")
    return spy == "long" and qqq == "long"


def drawdown(equity, high_water_mark):
    if high_water_mark <= 0:
        return 0.0
    return (high_water_mark - equity) / high_water_mark


def hit_max_drawdown(equity, high_water_mark, max_dd):
    """고점 대비 max_dd 이상 떨어지면 True."""
    return drawdown(equity, high_water_mark) >= max_dd


def update_trailing_stop(current_stop, price, atr_value, side, atr_mult):
    """트레일링 스톱은 유리한 방향으로만 이동한다(하드 스톱은 뒤로 안 밀림)."""
    if side == "long":
        candidate = price - atr_mult * atr_value
        return max(current_stop, candidate)
    candidate = price + atr_mult * atr_value
    return min(current_stop, candidate)
