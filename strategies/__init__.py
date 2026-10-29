"""전략 레지스트리. 자산 설정의 strategy 이름으로 함수를 찾는다."""
from strategies import mean_reversion, breakout, trend_following

REGISTRY = {
    "mean_reversion": mean_reversion.signal,
    "breakout": breakout.signal,
    "trend_following": trend_following.signal,
}


def get(name):
    if name not in REGISTRY:
        raise ValueError(f"알 수 없는 전략: {name}")
    return REGISTRY[name]
