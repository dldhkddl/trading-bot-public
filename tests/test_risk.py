"""리스크 규칙 검증: 1% 사이징, 손절 거리 우선, 낙폭 정지, 상관 필터."""
import math
from risk import manager as risk


def test_position_size_is_one_percent_of_equity():
    equity = 100_000
    entry, stop = 100.0, 95.0        # 손절 거리 5
    qty = risk.position_size(equity, 0.01, entry, stop)
    # 위험 예산 1000 / 거리 5 = 200주. 200주 x 5 = 1000 = 자기자본의 1%
    assert math.isclose(qty, 200.0)
    assert math.isclose(qty * (entry - stop), equity * 0.01)


def test_position_size_zero_when_no_stop_distance():
    assert risk.position_size(100_000, 0.01, 100.0, 100.0) == 0.0
    assert risk.position_size(100_000, 0.01, 100.0, 101.0) >= 0.0  # 거리는 절댓값


def test_stop_distance_drives_quantity_not_the_other_way():
    # 손절이 넓을수록 수량이 작아져야 위험이 일정하게 유지된다
    q_tight = risk.position_size(100_000, 0.01, 100.0, 99.0)   # 거리 1
    q_wide = risk.position_size(100_000, 0.01, 100.0, 90.0)    # 거리 10
    assert q_tight > q_wide
    assert math.isclose(q_tight * 1.0, q_wide * 10.0)


def test_max_drawdown_stop_triggers_at_ten_percent():
    hwm = 100_000
    assert not risk.hit_max_drawdown(91_000, hwm, 0.10)   # 9% 하락 → 안 멈춤
    assert risk.hit_max_drawdown(90_000, hwm, 0.10)       # 10% 하락 → 멈춤
    assert risk.hit_max_drawdown(85_000, hwm, 0.10)


def test_correlation_filter_blocks_btc_long():
    both_long = {"SPY": {"side": "long"}, "QQQ": {"side": "long"}}
    assert risk.correlation_blocks_btc_long(both_long) is True
    one_long = {"SPY": {"side": "long"}, "QQQ": {"side": "short"}}
    assert risk.correlation_blocks_btc_long(one_long) is False
    assert risk.correlation_blocks_btc_long(both_long, enabled=False) is False


def test_trailing_stop_only_moves_favorably():
    # 롱: 스톱은 올라가기만
    assert risk.update_trailing_stop(95.0, 110.0, 2.0, "long", 2.0) == 106.0
    assert risk.update_trailing_stop(106.0, 100.0, 2.0, "long", 2.0) == 106.0
